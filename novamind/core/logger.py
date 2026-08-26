"""
NovaMind 审计日志模块

提供异步、线程安全的JSONL日志写入。
每次智能体交互的所有关键步骤都会被记录：
  - LLM输入（包含消息数量）
  - 工具调用（名称+参数）
  - 工具执行结果
  - AI回复内容
  - 系统动作（心跳触发、上下文裁剪等）

日志格式：每行一个JSON对象，便于流式处理和grep查询。

队列策略：
  - 有界队列（默认 10000，可通过 NOVAMIND_LOG_QUEUE_SIZE 环境变量覆盖）
  - 队列满时按事件优先级驱逐，关键事件不会被丢弃：
    - critical（policy_violation, system_action 等）：绝不丢弃
    - normal（tool_call, tool_result, ai_message 等）：可被驱逐
    - low（llm_input, token_usage, context_pack_loaded）：优先驱逐
"""
import os
import json
import threading
import time
import atexit
import re
from collections import deque
from datetime import datetime, timezone
from .config import LOG_DIR


SENSITIVE_KEY_PATTERN = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|authorization|credential)",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|Bearer\s+[A-Za-z0-9._\-]{16,})",
    re.IGNORECASE,
)
MAX_LOG_STRING_LENGTH = 2000


def _sanitize_for_log(value, key: str = ""):
    """Recursively redact sensitive log fields and cap large values."""
    if key and SENSITIVE_KEY_PATTERN.search(key):
        return "[REDACTED]"

    if isinstance(value, dict):
        return {str(k): _sanitize_for_log(v, str(k)) for k, v in value.items()}

    if isinstance(value, list):
        return [_sanitize_for_log(item) for item in value[:50]]

    if isinstance(value, tuple):
        return tuple(_sanitize_for_log(item) for item in value[:50])

    if isinstance(value, str):
        redacted = SECRET_VALUE_PATTERN.sub("[REDACTED]", value)
        if len(redacted) > MAX_LOG_STRING_LENGTH:
            return redacted[:MAX_LOG_STRING_LENGTH] + "...[TRUNCATED]"
        return redacted

    return value


class AuditLogger:
    """
    审计日志器（单例模式）

    使用有界 deque + 守护线程实现异步写入，不阻塞主流程。
    程序退出时会flush队列中剩余的日志。

    背压策略：
      队列满时按事件优先级驱逐，关键事件不会被挤掉：
      - critical（policy_violation, system_action 等）：绝不丢弃
      - normal（tool_call, tool_result, ai_message 等）：可被驱逐
      - low（llm_input, token_usage, context_pack_loaded）：优先驱逐
    """
    _instance = None
    _lock = threading.Lock()

    # 关键事件：策略违规、系统动作、错误，队列满时绝不丢弃
    CRITICAL_EVENTS = frozenset({
        "policy_violation",
        "policy_check",
        "system_action",
        "summary_evaluation",
    })

    # 低优先级事件：常规统计类，队列满时优先驱逐
    LOW_PRIORITY_EVENTS = frozenset({
        "llm_input",
        "token_usage",
        "context_pack_loaded",
    })

    def __new__(cls, log_dir: str | None = None):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_logger(log_dir or LOG_DIR)
            elif log_dir and log_dir != cls._instance.log_dir:
                print(
                    f"[NovaMind Logger] 注意: 自定义log_dir='{log_dir}' 被忽略, "
                    f"使用首次初始化的 '{cls._instance.log_dir}'"
                )
            return cls._instance

    def _init_logger(self, log_dir: str):
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)

        # 有界双端队列：maxlen 自动限制容量
        maxsize = int(os.getenv("NOVAMIND_LOG_QUEUE_SIZE", "10000"))
        self._buffer: deque = deque(maxlen=maxsize)
        self._buffer_lock = threading.Lock()
        self._not_empty = threading.Condition(self._buffer_lock)

        # 守护线程：后台持续消费队列并写入文件
        self._worker = threading.Thread(target=self._write_loop, daemon=True)
        self._worker.start()

        # 确保程序关闭时队列中的日志能写完
        atexit.register(self.shutdown)

        # 背压统计：丢弃计数 + 限频警告
        self._dropped_count = 0
        self._dropped_by_event: dict[str, int] = {}
        self._last_warn_ts = 0.0
        self._stopped = False

    def _event_priority(self, event: str) -> int:
        """返回事件优先级：0=最高（绝不丢），2=最低（优先丢）。"""
        if event in self.CRITICAL_EVENTS:
            return 0
        if event in self.LOW_PRIORITY_EVENTS:
            return 2
        return 1

    def _write_loop(self):
        """后台写入死循环：盯着队列，有日志就写，没日志就等待"""
        while True:
            with self._not_empty:
                while not self._buffer:
                    self._not_empty.wait()
                log_item = self._buffer.popleft()

            if log_item is None:
                break  # 毒丸，退出

            try:
                thread_id = log_item.get("thread_id", "system")
                # 文件名安全化：只保留字母数字和横杠下划线
                safe_id = "".join(c for c in thread_id if c.isalnum() or c in "-_") or "default"
                file_path = os.path.join(self.log_dir, f"{safe_id}.jsonl")

                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(log_item, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"[NovaMind Logger] 异步写日志失败: {e}")

    def _try_evict_for_new(self, new_priority: int) -> bool:
        """
        尝试驱逐一条优先级低于 new_priority 的最旧非关键事件，为新事件腾位。

        必须在持有 _buffer_lock 的情况下调用。
        Returns: True 表示成功驱逐腾位，False 表示没有可驱逐项
        """
        if len(self._buffer) < self._buffer.maxlen:
            return True  # 还有空间，不需要驱逐

        # 扫描队列，找最低优先级的最旧项来驱逐
        best_idx = None
        best_prio = new_priority  # 只驱逐优先级严格低于新事件的
        for i, item in enumerate(self._buffer):
            if item is None:
                continue  # 毒丸，跳过
            prio = self._event_priority(item.get("event", ""))
            if prio > best_prio:
                best_prio = prio
                best_idx = i
                if prio == 2:
                    break  # 已经找到最低优先级

        if best_idx is None:
            return False

        # 驱逐该项：deque 没有 O(1) 中间删除，重建一次
        evicted = self._buffer[best_idx]
        remaining = [x for j, x in enumerate(self._buffer) if j != best_idx]
        self._buffer.clear()
        self._buffer.extend(remaining)

        self._dropped_count += 1
        evict_event = evicted.get("event", "unknown") if evicted else "poison_pill"
        self._dropped_by_event[evict_event] = (
            self._dropped_by_event.get(evict_event, 0) + 1
        )
        return True

    def log_event(self, thread_id: str, event: str, **kwargs):
        """
        记录一个审计事件

        Args:
            thread_id: 会话线程ID
            event: 事件类型（llm_input, tool_call, tool_result, ai_message, system_action）
            **kwargs: 事件附加数据

        背压策略：队列满时按优先级驱逐最低优先级的非关键事件，
        关键事件（policy_violation, system_action 等）绝不丢弃。
        """
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sanitized_kwargs = {
            key: _sanitize_for_log(value, key)
            for key, value in kwargs.items()
        }
        log_item = {
            "ts": now_utc,
            "thread_id": thread_id,
            "event": event,
            **sanitized_kwargs,
        }

        new_priority = self._event_priority(event)

        with self._not_empty:
            if len(self._buffer) >= self._buffer.maxlen:
                # 队列满：尝试驱逐一条优先级更低的非关键事件
                evicted = self._try_evict_for_new(new_priority)

                if not evicted:
                    # 没有可驱逐项（全是关键事件）
                    if new_priority == 0:
                        # 新事件也是关键的：驱逐最旧的一条腾位
                        if self._buffer:
                            dropped = self._buffer.popleft()
                            self._dropped_count += 1
                            d_event = dropped.get("event", "unknown") if dropped else "poison_pill"
                            self._dropped_by_event[d_event] = (
                                self._dropped_by_event.get(d_event, 0) + 1
                            )
                    else:
                        # 非关键事件，队列全是关键事件：丢弃新事件本身
                        self._dropped_count += 1
                        self._dropped_by_event[event] = (
                            self._dropped_by_event.get(event, 0) + 1
                        )

                        # 限频警告
                        now = time.time()
                        if now - self._last_warn_ts >= 60:
                            self._last_warn_ts = now
                            print(
                                f"[NovaMind Logger] ⚠️ 审计日志队列已满且全为关键事件，"
                                f"已丢弃 {self._dropped_count} 条事件（累计计数）"
                            )
                        return

                # 限频警告
                now = time.time()
                if now - self._last_warn_ts >= 60:
                    self._last_warn_ts = now
                    print(
                        f"[NovaMind Logger] ⚠️ 审计日志队列已满，已丢弃 "
                        f"{self._dropped_count} 条事件（累计计数）"
                    )

            self._buffer.append(log_item)
            self._not_empty.notify()

    def shutdown(self):
        """关闭日志器：发送毒丸并等待队列清空"""
        # 防止重复 shutdown 导致死锁
        if self._stopped:
            return
        self._stopped = True

        # 发送毒丸并等待工作线程处理完剩余事件
        with self._not_empty:
            self._buffer.append(None)
            self._not_empty.notify_all()

        self._worker.join(timeout=10)

        if self._dropped_count > 0:
            breakdown = ", ".join(
                f"{k}={v}" for k, v in sorted(
                    self._dropped_by_event.items(), key=lambda x: -x[1]
                )
            )
            print(
                f"[NovaMind Logger] 关闭时统计：本次运行共丢弃 "
                f"{self._dropped_count} 条审计事件"
                f"（按事件: {breakdown}）"
            )


# 全局审计日志实例
audit_logger = AuditLogger()
