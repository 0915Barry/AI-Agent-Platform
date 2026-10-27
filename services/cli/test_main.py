#!/usr/bin/env python3
"""agentctl 的 HTTP、SSE、会话文件和展示测试。"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import main as cli


class MockControlPlane(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_json(self, status: int, payload: dict, cookie: str | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path == "/api/auth/login":
            self.send_json(
                200,
                {"user": {"username": "demo"}, "expiresAt": 123456},
                "aap_session=test-token; Path=/; HttpOnly",
            )
            return
        self.send_json(404, {"error": {"code": "missing", "message": "missing"}})

    def do_GET(self) -> None:
        if self.path.endswith("/stream"):
            body = (
                'id: 1\nevent: task-event\ndata: {"sequence":1,"type":"text",'
                '"data":{"delta":"你好"}}\n\n'
                'id: 2\nevent: task-event\ndata: {"sequence":2,"type":"completed",'
                '"data":{"output":"你好"}}\n\n'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_json(404, {"error": {"code": "missing", "message": "missing"}})


class AgentCtlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockControlPlane)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_login_response_and_cookie_are_parsed(self) -> None:
        payload, headers = cli.ApiClient(self.base_url).json(
            "POST", "/api/auth/login", {"username": "demo", "password": "password"}
        )
        self.assertEqual(payload["user"]["username"], "demo")
        self.assertEqual(cli.extract_cookie(headers), "aap_session=test-token")

    def test_sse_events_are_decoded(self) -> None:
        events = list(cli.ApiClient(self.base_url, "aap_session=test-token").events("/stream"))
        self.assertEqual(events[0]["data"]["delta"], "你好")
        self.assertEqual(events[-1]["type"], "completed")

    def test_session_file_is_private_and_removable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = cli.SessionStore(path)
            store.save({"cookie": "aap_session=secret", "baseUrl": self.base_url})
            self.assertEqual(store.load()["cookie"], "aap_session=secret")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            store.clear()
            self.assertFalse(path.exists())

    def test_task_events_have_distinct_terminal_output(self) -> None:
        output = io.StringIO()
        console = cli.Console(False, output)
        client = cli.ApiClient(self.base_url, "aap_session=test-token")
        cli.render_task_events(client, "agent-demo", "task-demo", console)
        rendered = output.getvalue()
        self.assertIn("Pi > 你好", rendered)
        self.assertEqual(rendered.count("你好"), 1)


if __name__ == "__main__":
    unittest.main()
