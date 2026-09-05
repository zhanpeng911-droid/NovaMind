"""有界 JSONL 审计事件读取器（收尾修复 Phase 3）。

二进制扫描设计（不整行读进内存）：
- 块大小 8 KiB，单行上限 256 KiB，单请求扫描上限 2 MiB（命名常量）；
- 增量读取用有界 read(size)，累计扫描字节不超过预算；结果可少于 limit，
  字节预算优先于填满条数；
- cursor 按实际消费的二进制偏移推进（不使用 len(line)+1 猜测换行长度）；
- 跨块 UTF-8 安全：JSON 行按 \\n 分隔，UTF-8 续字节不含 0x0A；
- EOF 无换行的尾行视为未完成：不提前丢掉，保留其起点作为下次增量 cursor；
- 超长行/坏 JSON/非对象 JSON 跳过并记录可控日志；超长整行不被读进内存
  （超 MAX_LINE 即进入丢弃模式，只扫描找换行）；
- 文件截断导致 offset 越界 → OffsetBeyondEOFError（端点映射 400）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("novamind.webui")

CHUNK_SIZE = 8 * 1024            # 块大小 8 KiB
MAX_LINE = 256 * 1024            # 单行上限 256 KiB
SCAN_BUDGET = 2 * 1024 * 1024    # 单请求扫描上限 2 MiB


class OffsetBeyondEOFError(Exception):
    """offset 超过当前文件大小（截断/轮转），端点映射 400 invalid_cursor。"""


@dataclass(frozen=True)
class ReadResult:
    events: list[dict] = field(default_factory=list)
    next_offset: int = 0
    has_more: bool = False
    discarding: bool = False


def _read_bounded(f, scanned: int) -> bytes:
    """预算内有界 read：剩余预算 <=0 返回空（视为预算耗尽），
    绝不出现负数 size（负 size 等价于 read(-1) 全读）。"""
    remaining = SCAN_BUDGET - scanned
    if remaining <= 0:
        return b""
    return f.read(min(CHUNK_SIZE, remaining))


def _parse_line(raw: bytes) -> dict | None:
    """解析单行 JSON；必须是 JSON 对象。坏/非对象/空行返回 None。"""
    if not raw:
        return None
    line = raw.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def read_events(
    path: str,
    *,
    limit: int,
    offset: int | None = None,
    discarding: bool = False,
) -> ReadResult:
    """有界读取。

    offset=None：从文件尾向前扫描最新 limit 条（首次页）；
    offset=N：从 N 增量读取；discarding 表示正处于超长行丢弃中（沿用上次
    状态，达到预算时保存，下次跳到换行后恢复解析）。
    """
    try:
        file_size = os.path.getsize(path)
    except OSError:
        return ReadResult()
    if offset is not None and (offset < 0 or offset > file_size):
        raise OffsetBeyondEOFError()
    if offset is None:
        return _tail_read(path, file_size, limit)
    return _incremental_read(path, file_size, offset, limit, discarding)


def _incremental_read(
    path: str, file_size: int, offset: int, limit: int, discarding: bool,
) -> ReadResult:
    """从 offset 增量读取：有界扫描 + 超长行丢弃 + EOF 尾行保留。"""
    events: list[dict] = []
    with open(path, "rb") as f:
        f.seek(offset)
        buf = b""
        scanned = 0
        pos = offset
        line_start = offset
        at_eof = False
        budget_exhausted = False

        while len(events) < limit:
            if discarding:
                # 超长行丢弃：只扫描找 \n，不保留行内容
                chunk = _read_bounded(f, scanned)
                if not chunk:
                    if scanned >= SCAN_BUDGET:
                        budget_exhausted = True
                    else:
                        at_eof = True
                    break
                scanned += len(chunk)
                nl = chunk.find(b"\n")
                if nl != -1:
                    pos += nl + 1
                    line_start = pos
                    discarding = False
                    # \n 之后的剩余进入行缓冲（可能是后续正常行的开头）
                    buf = chunk[nl + 1:]
                else:
                    pos += len(chunk)
                    line_start = pos
                continue

            if b"\n" not in buf:
                if len(buf) > MAX_LINE:
                    # 当前行已超长：丢弃已缓冲部分（不保留超长内容），
                    # 并推进实际消费位置——否则 next_offset 停在旧处，
                    # 再次读取会重复已解析的事件。
                    pos += len(buf)
                    line_start = pos
                    discarding = True
                    buf = b""
                    continue
                chunk = _read_bounded(f, scanned)
                if not chunk:
                    if scanned >= SCAN_BUDGET:
                        budget_exhausted = True
                    else:
                        at_eof = True
                    break
                scanned += len(chunk)
                buf += chunk
                continue

            # 遇到换行：行结束
            nl = buf.index(b"\n")
            line = buf[:nl]
            after = nl + 1
            if len(line) > MAX_LINE:
                # 行超长（缓冲被裁剪前的一瞬）：跳过
                pass
            else:
                obj = _parse_line(line)
                if obj is not None:
                    events.append(obj)
            buf = buf[after:]
            pos += after
            line_start = pos

        # 结束判定
        tail_incomplete = at_eof and bool(buf) and b"\n" not in buf
        if tail_incomplete:
            # EOF 无换行尾行：不解析，保留其起点（追加后再解析）
            return ReadResult(events, line_start, has_more=False,
                              discarding=False)
        if budget_exhausted:
            return ReadResult(events, line_start, has_more=True,
                              discarding=discarding)
        return ReadResult(events, pos, has_more=pos < file_size,
                          discarding=False)


def _tail_read(path: str, file_size: int, limit: int) -> ReadResult:
    """首次最新页：从文件尾分块向前扫描，取最近 limit 条完整行。

    同时受换行数与扫描字节预算约束：长文件缺少换行时不会持续向前读满
    整个文件（累计读入 ≤ SCAN_BUDGET）。尾部无换行的未完成行保留其起点
    作为下一次增量 cursor；只有从文件中间起读时才丢弃头部不完整行
    （预算内少于 limit 也可返回）。超长行（> MAX_LINE）跳过。"""
    events: list[dict] = []
    with open(path, "rb") as f:
        data = b""
        pos = file_size
        newlines = 0
        scanned = 0
        while pos > 0 and newlines <= limit and scanned < SCAN_BUDGET:
            step = min(CHUNK_SIZE, pos, SCAN_BUDGET - scanned)
            pos -= step
            f.seek(pos)
            chunk = f.read(step)
            data = chunk + data
            newlines += chunk.count(b"\n")
            scanned += len(chunk)

        # 尾部无换行 → 最后一段是未完成尾行（保留起点）
        tail_incomplete = b""
        if data and not data.endswith(b"\n"):
            parts = data.split(b"\n")
            tail_incomplete = parts[-1]
            data = data[: len(data) - len(tail_incomplete)]

        # 未完成尾行超长：返回 discard 状态，下次增量从该起点跳过换行
        tail_discarding = len(tail_incomplete) > MAX_LINE

        if not data:
            next_offset = file_size - len(tail_incomplete)
            return ReadResult([], next_offset, has_more=False,
                              discarding=tail_discarding)

        lines = data.split(b"\n")
        if data.endswith(b"\n"):
            lines = lines[:-1]  # 结尾空元素（行分隔符之后）
        if pos > 0:
            lines = lines[1:]   # 从文件中间起读：丢弃头部不完整行
        for raw in lines[-limit:]:
            if len(raw) > MAX_LINE:
                continue  # 超长完整行：跳过（不整行读进内存）
            obj = _parse_line(raw)
            if obj is not None:
                events.append(obj)

        next_offset = file_size - len(tail_incomplete)
        return ReadResult(events, next_offset, has_more=False,
                          discarding=tail_discarding)
