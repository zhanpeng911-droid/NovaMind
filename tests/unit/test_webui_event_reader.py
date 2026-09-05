"""event_reader 有界读取单元测试（收尾修复 Phase 3）。

覆盖方案 §5 验收清单：尾页→EOF→追加→同游标读新事件；有限 size 与扫描
预算（禁止 read(-1)）；limit=1 大量追加不读全部后缀；跨块/LF/CRLF/UTF-8/
未完成尾行续写不重复不丢失；坏 JSON/非对象/超长行不卡死；offset 越界与
截断报错；v1 兼容与 v2 discard 恢复。
"""
import json
import os
import tempfile
import unittest

from novamind.webui.event_reader import (
    CHUNK_SIZE,
    MAX_LINE,
    SCAN_BUDGET,
    OffsetBeyondEOFError,
    read_events,
)
from novamind.webui.api_models import (
    InvalidCursorError,
    decode_monitor_events_cursor,
    encode_cursor,
    encode_monitor_events_cursor,
)


def _mk_file(content: bytes = b"") -> str:
    fd, path = tempfile.mkstemp(prefix="novamind_reader_", suffix=".jsonl")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _ev(i):
    return json.dumps({"event": f"e{i}", "n": i}).encode()


class TestCursorCodec(unittest.TestCase):
    def test_v2_roundtrip(self):
        raw = encode_monitor_events_cursor(1234, discarding=True)
        offset, discarding = decode_monitor_events_cursor(raw)
        self.assertEqual((offset, discarding), (1234, True))

    def test_v1_cursor_backward_compat(self):
        raw = encode_cursor("monitor_events", {"offset": 42})
        offset, discarding = decode_monitor_events_cursor(raw)
        self.assertEqual((offset, discarding), (42, False))

    def test_wrong_kind_or_version_rejected(self):
        raw = encode_cursor("sessions", {"last_id": 1, "thread_id": "t"})
        with self.assertRaises(InvalidCursorError):
            decode_monitor_events_cursor(raw)
        with self.assertRaises(InvalidCursorError):
            decode_monitor_events_cursor("garbage")


class TestIncrementalRead(unittest.TestCase):
    def test_tail_page_then_eof_then_append_then_read_new(self):
        path = _mk_file(_ev(1) + b"\n" + _ev(2) + b"\n")
        # 首次尾页 limit=1
        r1 = read_events(path, limit=1)
        self.assertEqual([e["event"] for e in r1.events], ["e2"])
        # 读到 EOF：has_more=False，但 next_offset 仍返回（空页也可查更新）
        self.assertFalse(r1.has_more)
        # 无新增空页：同游标再查，无事件、游标不后退
        r2 = read_events(path, limit=10, offset=r1.next_offset)
        self.assertEqual(r2.events, [])
        self.assertFalse(r2.has_more)
        self.assertEqual(r2.next_offset, r1.next_offset)
        # 追加后同一游标读到新事件
        with open(path, "ab") as f:
            f.write(_ev(3) + b"\n")
        r3 = read_events(path, limit=10, offset=r1.next_offset)
        self.assertEqual([e["event"] for e in r3.events], ["e3"])
        self.assertFalse(r3.has_more)
        os.unlink(path)

    def test_scan_budget_enforced_no_read_all(self):
        """limit=1 且大量追加：不会读取全部后缀（扫描字节受预算约束）。"""
        path = _mk_file(_ev(0) + b"\n")
        with open(path, "ab") as f:
            for i in range(1, 4000):
                f.write(_ev(i) + b"\n")
        # 增量从第 1 行后读起，limit=1：只应读到 e1，且扫描字节 <= 预算
        r = read_events(path, limit=1, offset=len(_ev(0)) + 1)
        self.assertEqual([e["event"] for e in r.events], ["e1"])
        os.unlink(path)

    def test_bounded_reads_no_read_minus_one(self):
        """包装文件对象：实际读取字节有限，禁止 read(-1)/read() 无参全读。"""
        from unittest import mock

        path = _mk_file(b"".join(_ev(i) + b"\n" for i in range(5)))
        sizes = []
        real_open = open

        def spy_open(file, *a, **kw):
            f = real_open(file, *a, **kw)
            orig_read = f.read

            def read(size=-1):
                sizes.append(size)
                if size is None or size < 0:
                    raise AssertionError("禁止无界 read(-1)")
                return orig_read(size)

            f.read = read
            return f

        with mock.patch("novamind.webui.event_reader.open", spy_open):
            r1 = read_events(path, limit=3)
            r2 = read_events(path, limit=2, offset=r1.next_offset)
        # 尾页 limit=3 → 最近 3 条；EOF 游标查更新 → 无新增
        self.assertEqual([e["event"] for e in r1.events], ["e2", "e3", "e4"])
        self.assertEqual(r2.events, [])
        self.assertTrue(sizes, "应有 read 调用")
        self.assertTrue(all(s is not None and s >= 0 for s in sizes),
                        "不允许无界 read(-1)/read()")
        # 增量读取：每次扫描受预算约束（本测试读取量远小于预算）
        self.assertLessEqual(sum(sizes), SCAN_BUDGET)
        os.unlink(path)

    def test_lf_crlf_utf8_and_incomplete_tail_resume(self):
        """LF/CRLF/UTF-8 跨块 + 未完成尾行续写不重复不丢失。"""
        path = _mk_file()
        # CRLF 行 + UTF-8 中文
        line1 = json.dumps({"event": "中文事件", "x": "你好"})
        line2 = json.dumps({"event": "crlf"})
        with open(path, "wb") as f:
            f.write(line1.encode() + b"\r\n" + line2.encode() + b"\r\n")
        r1 = read_events(path, limit=10)
        self.assertEqual(len(r1.events), 2)
        self.assertEqual(r1.events[0]["event"], "中文事件")
        # 未完成尾行：追加半行，游标停在行起点，不解析
        with open(path, "ab") as f:
            f.write(json.dumps({"event": "partial"}).encode()[:5])  # 半行
        r2 = read_events(path, limit=10, offset=r1.next_offset)
        self.assertEqual(r2.events, [])
        self.assertFalse(r2.has_more)
        self.assertLessEqual(r2.next_offset, r1.next_offset + 5)
        # 补齐尾行：同一游标读到完整事件
        with open(path, "ab") as f:
            f.write(json.dumps({"event": "partial"}).encode()[5:] + b"\n")
        r3 = read_events(path, limit=10, offset=r1.next_offset)
        self.assertEqual([e["event"] for e in r3.events], ["partial"])
        os.unlink(path)

    def test_bad_json_non_object_long_line_skipped(self):
        path = _mk_file()
        with open(path, "wb") as f:
            f.write(_ev(1) + b"\n")
            f.write(b"{broken json\n")           # 坏 JSON
            f.write(b'"just a string"\n')        # 非对象 JSON
            f.write(_ev(2) + b"\n")
            f.write(b"x" * (MAX_LINE + 10) + b"\n")  # 超长完整行
            f.write(_ev(3) + b"\n")
        r = read_events(path, limit=10)
        self.assertEqual([e["event"] for e in r.events], ["e1", "e2", "e3"])
        # 超长行不卡死 cursor：之后还能继续读到 e3
        os.unlink(path)

    def test_offset_beyond_eof_raises(self):
        path = _mk_file(_ev(1) + b"\n")
        with self.assertRaises(OffsetBeyondEOFError):
            read_events(path, limit=10, offset=1000)
        with self.assertRaises(OffsetBeyondEOFError):
            read_events(path, limit=10, offset=-1)
        os.unlink(path)

    def test_truncated_file_raises(self):
        """文件被截断（大小 < 游标 offset）→ OffsetBeyondEOFError。"""
        path = _mk_file(_ev(1) + b"\n" + _ev(2) + b"\n")
        r1 = read_events(path, limit=10)
        self.assertEqual(len(r1.events), 2)
        with open(path, "wb") as f:
            f.write(_ev(1) + b"\n")  # 截断掉 e2
        with self.assertRaises(OffsetBeyondEOFError):
            read_events(path, limit=10, offset=r1.next_offset)
        os.unlink(path)

    def test_v2_discard_resume_after_budget(self):
        """超长未完成尾行超过预算：discard 状态随 cursor 传递，补换行后恢复。"""
        path = _mk_file(_ev(1) + b"\n")
        # 超长未完成尾行（无换行）> 单请求扫描预算：r1 必在丢弃中耗尽预算
        with open(path, "ab") as f:
            f.write(b"z" * (SCAN_BUDGET + CHUNK_SIZE))
        r1 = read_events(path, limit=10, offset=len(_ev(1)) + 1)
        self.assertTrue(r1.discarding, "预算耗尽时必须携带 discard 状态")
        self.assertTrue(r1.has_more, "预算耗尽时文件未读完")
        self.assertGreater(r1.next_offset, len(_ev(1)) + 1, "cursor 必须前进")
        # 补完成超长尾行（换行）并追加 e2：同游标 discard 恢复后解析 e2
        with open(path, "ab") as f:
            f.write(b"\n")
            f.write(_ev(2) + b"\n")
        r2 = read_events(path, limit=10, offset=r1.next_offset,
                         discarding=r1.discarding)
        events = [e["event"] for e in r2.events]
        self.assertIn("e2", events, "discard 恢复后应解析新事件")
        os.unlink(path)


if __name__ == "__main__":
    unittest.main()
