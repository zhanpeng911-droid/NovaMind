"""Disposable, offline UI fixture. No NovaMind imports, credentials or disk state.

Run: python tests/manual/gui_preview.py
Serves the real static UI at http://127.0.0.1:8981 with synthetic API data.
This checks presentation and wiring, not the model/backend implementation.
"""
import json
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

STATIC = Path(__file__).resolve().parents[2] / "novamind/webui/static"
SESSIONS = [{"thread_id": "ui_sample", "title": "梳理项目的核心调用链"},
            {"thread_id": "ui_notes", "title": "为一个想法写下实现方案"}]
MESSAGES = [{"role": "user", "content": "帮我梳理这个项目的核心调用链。"},
            {"role": "ai", "content": "## 从请求到结果\n这是界面测试的模拟内容，不是真实项目分析。\n\n- 请求进入服务层，由 Agent 协调工具执行。\n- 执行记录可以在监控面板查看。\n\n```python\nasync def run_task(message):\n    return await agent.run(message)\n```\n\n下一步可以一起查看具体实现。", "tools": ["read_file", "list_files"]}]
PAGE = {"next_cursor": "ui_next", "has_more": False}


class Preview(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def json_response(self, body):
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/sessions":
            self.json_response({"sessions": SESSIONS, "pagination": PAGE})
        elif path.startswith("/history/"):
            self.json_response({"messages": MESSAGES, "pagination": PAGE})
        elif path == "/monitor/sessions":
            self.json_response({"sessions": [{"thread_id": "ui_sample", "size_bytes": 2400}], "pagination": PAGE})
        elif path.startswith("/monitor/events/"):
            events = [] if "cursor=" in self.path else [
                {"event": "llm_input", "message_count": 4},
                {"event": "tool_call", "tool": "read_file", "args": {"path": "example.py"}},
                {"event": "tool_result", "tool": "read_file", "result_summary": "读取完成（模拟）"},
                {"event": "token_usage", "prompt_tokens": 240, "completion_tokens": 80, "total_tokens": 320},
                {"event": "ai_message", "content": "分析完成（模拟）"}]
            self.json_response({"events": [{**e, "ts": "2026-09-06T10:24:08Z"} for e in events], "pagination": PAGE})
        elif path == "/doctor":
            self.json_response({"ok": False, "counts": {"error": 0, "warning": 1, "info": 1}, "findings": [
                {"level": "warning", "code": "PREVIEW_ONLY", "message": "当前为独立界面测试环境", "suggestion": "所有数据均为模拟，未连接模型或真实数据库。"},
                {"level": "info", "code": "LOCAL_PREVIEW", "message": "静态资源已就绪", "suggestion": "可验证导航、主题、输入和响应布局。"}]})
        elif path == "/skills":
            skills = [{"name": name, "description": desc, "selections": 12, "applied": 10, "completions": 9, "effective_rate": .75}
                      for name, desc in [("code-review", "检查代码中的边界条件，让每一次修改都有依据。"),
                                         ("file-edit", "围绕明确的目标，进行小而可靠的文件修改。"),
                                         ("research", "从问题出发，整理信息与可追溯的结论。")]]
            self.json_response({"skills": skills, "count": len(skills), "pagination": PAGE})
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            super().do_GET()

    def do_DELETE(self):
        thread_id = urlparse(self.path).path.rsplit("/", 1)[-1]
        SESSIONS[:] = [row for row in SESSIONS if row["thread_id"] != thread_id]
        self.json_response({"status": "ok"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        thread_id = body.get("thread_id") or "ui_" + uuid.uuid4().hex[:8]
        if not any(row["thread_id"] == thread_id for row in SESSIONS):
            SESSIONS.insert(0, {"thread_id": thread_id, "title": body.get("message", "模拟任务")[:24]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        frames = [{"type": "thread", "thread_id": thread_id},
                  {"type": "tool", "name": "read_file"},
                  {"type": "text", "content": "这是模拟的界面回复。\n\n**执行记录**和停止状态均使用原有前端逻辑呈现。"},
                  {"type": "done"}]
        if "错误" in body.get("message", ""):
            frames[2] = {"type": "error", "code": "preview_error", "message": "模拟服务端错误：请重试", "request_id": "ui_fixture"}
        try:
            for frame in frames:
                self.wfile.write(("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


if __name__ == "__main__":
    print("Offline UI fixture: http://127.0.0.1:8981 (synthetic data only)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8981), Preview).serve_forever()
