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
"""
import os
import json
import threading
import queue
import atexit
import re
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

    使用内存队列+守护线程实现异步写入，不阻塞主流程。
    程序退出时会flush队列中剩余的日志。
    """
    _instance = None
    _lock = threading.Lock()

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

        # 无界内存队列，缓冲日志事件
        self.log_queue: queue.Queue = queue.Queue()

        # 守护线程：后台持续消费队列并写入文件
        self._worker = threading.Thread(target=self._write_loop, daemon=True)
        self._worker.start()

        # 确保程序关闭时队列中的日志能写完
        atexit.register(self.shutdown)

    def _write_loop(self):
        """后台写入死循环：盯着队列，有日志就写，没日志就阻塞"""
        while True:
            log_item = self.log_queue.get()
            if log_item is None:
                self.log_queue.task_done()
                break

            try:
                thread_id = log_item.get("thread_id", "system")
                # 文件名安全化：只保留字母数字和横杠下划线
                safe_id = "".join(c for c in thread_id if c.isalnum() or c in "-_") or "default"
                file_path = os.path.join(self.log_dir, f"{safe_id}.jsonl")

                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(log_item, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"[NovaMind Logger] 异步写日志失败: {e}")
            finally:
                self.log_queue.task_done()

    def log_event(self, thread_id: str, event: str, **kwargs):
        """
        记录一个审计事件

        Args:
            thread_id: 会话线程ID
            event: 事件类型（llm_input, tool_call, tool_result, ai_message, system_action）
            **kwargs: 事件附加数据
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
        self.log_queue.put(log_item)

    def shutdown(self):
        """关闭日志器：发送毒丸并等待队列清空"""
        self.log_queue.put(None)
        self.log_queue.join()


# 全局审计日志实例
audit_logger = AuditLogger()
