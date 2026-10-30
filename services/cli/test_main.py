#!/usr/bin/env python3
"""agentctl 的 HTTP、SSE、会话文件和展示测试。"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import main as cli


class MockControlPlane(BaseHTTPRequestHandler):
    deleted_paths: list[str] = []

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
        parsed = urlsplit(self.path)
        if parsed.path == "/api/workspaces":
            self.send_json(
                200,
                {
                    "workspaces": [
                        {
                            "id": "workspace-aaaaaaaaaaaaaaaa",
                            "name": "测试工作区",
                            "conversationCount": 1,
                            "updatedAt": 2,
                            "current": True,
                        }
                    ]
                },
            )
            return
        if parsed.path == "/api/workspaces/workspace-aaaaaaaaaaaaaaaa/conversations":
            self.send_json(
                200,
                {
                    "conversations": [
                        {
                            "id": "conversation-bbbbbbbbbbbbbbbb",
                            "instanceId": "agent-demo",
                            "title": "测试会话",
                            "preview": "最后一条消息",
                            "updatedAt": 2,
                        }
                    ]
                },
            )
            return
        if parsed.path.endswith("/stream"):
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
        if parsed.path.endswith("/messages"):
            self.send_json(
                200,
                {
                    "messages": [
                        {"sequence": 1, "role": "user", "content": "旧问题", "createdAt": 1},
                        {
                            "sequence": 2,
                            "role": "assistant",
                            "content": "旧回答",
                            "createdAt": 2,
                        },
                    ],
                    "nextBefore": None,
                    "startupTiming": {
                        "totalMs": 100,
                        "host": {"guest_ready_ms": 80},
                        "guest": {"kernel_to_init": 40},
                    },
                },
            )
            return
        if parsed.path == "/api/instances/agent-demo/conversations":
            self.send_json(
                200,
                {
                    "conversations": [
                        {
                            "id": "conversation-demo",
                            "title": "演示会话",
                            "preview": "旧回答",
                            "updatedAt": 2,
                        }
                    ]
                },
            )
            return
        if parsed.path == "/api/instances/agent-demo/conversations/conversation-demo":
            self.send_json(200, {"id": "conversation-demo", "title": "演示会话"})
            return
        self.send_json(404, {"error": {"code": "missing", "message": "missing"}})

    def do_DELETE(self) -> None:
        self.deleted_paths.append(self.path)
        if self.path == "/api/workspaces/workspace-aaaaaaaaaaaaaaaa":
            self.send_json(
                200,
                {
                    "id": "workspace-aaaaaaaaaaaaaaaa",
                    "status": "deleted",
                    "currentWorkspace": {"name": "默认工作区"},
                },
            )
            return
        if self.path.endswith("/conversations/conversation-bbbbbbbbbbbbbbbb"):
            self.send_json(
                200,
                {"id": "conversation-bbbbbbbbbbbbbbbb", "status": "deleted"},
            )
            return
        self.send_json(404, {"error": {"code": "missing", "message": "missing"}})


class AgentCtlTests(unittest.TestCase):
    def setUp(self) -> None:
        MockControlPlane.deleted_paths = []
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

    def test_login_can_read_password_from_standard_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = cli.SessionStore(Path(directory) / "state.json")
            output = io.StringIO()
            args = SimpleNamespace(
                username="demo",
                password_stdin=True,
                base_url=self.base_url,
            )
            with patch("sys.stdin", io.StringIO("automation-password\n")):
                cli.command_login(args, store, cli.Console(False, output))
            self.assertEqual(store.load()["username"], "demo")
            self.assertIn("已登录：demo", output.getvalue())

    def test_help_explains_user_flow_and_diagnostic_commands(self) -> None:
        help_text = cli.build_parser().format_help()
        self.assertIn("./agentctl workspace", help_text)
        self.assertIn("./agentctl chat", help_text)
        self.assertIn("实例命令保留用于诊断", help_text)

    def test_dark_background_colors_use_bright_blue_and_red(self) -> None:
        console = cli.Console(True, io.StringIO())
        self.assertEqual(console.paint("系统", "blue"), "\033[94m系统\033[0m")
        self.assertEqual(console.paint("错误", "red"), "\033[91m错误\033[0m")
        self.assertEqual(console.paint("启动", "pink"), "\033[95m启动\033[0m")

    def test_startup_timing_is_rendered_one_item_per_line(self) -> None:
        output = io.StringIO()
        console = cli.Console(False, output)
        cli.render_startup_timing(
            {
                "totalMs": 11754,
                "host": {"guest_ready_ms": 11269},
                "guest": {"kernel_to_init": 3060},
            },
            console,
        )
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "[启动耗时]",
                "实例启动总耗时 = 11754 ms",
                "Guest启动并就绪 = 11269 ms",
                "内核启动并进入 init = 3060 ms",
            ],
        )

    def test_menu_preview_uses_ten_characters(self) -> None:
        self.assertEqual(cli.menu_preview("一二三四五六七八九十十一"), "一二三四五六七八九十…")
        self.assertEqual(cli.menu_preview(""), "暂无消息")

    def test_workspace_and_chat_default_to_user_facing_flow(self) -> None:
        parser = cli.build_parser()
        workspace = parser.parse_args(["workspace", "--new", "研发项目"])
        chat = parser.parse_args(["chat"])
        self.assertEqual(workspace.new_name, "研发项目")
        self.assertIsNone(chat.instance_id)
        self.assertIsNone(chat.workspace)

    def test_workspace_and_conversation_can_be_deleted_from_submenus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = cli.SessionStore(Path(directory) / "state.json")
            store.save({"cookie": "aap_session=test-token", "baseUrl": self.base_url})
            console = cli.Console(False, io.StringIO())
            workspace_args = SimpleNamespace(
                base_url=self.base_url,
                new_name=None,
                select_id=None,
            )
            with patch.object(cli, "choose_option", side_effect=[1, 1]), patch(
                "builtins.input", return_value="测试工作区"
            ):
                cli.command_workspace(workspace_args, store, console)

            chat_args = SimpleNamespace(
                base_url=self.base_url,
                instance_id=None,
                conversation=None,
                workspace=None,
                history_limit=10,
            )
            with patch.object(cli, "choose_option", side_effect=[1, 1]), patch(
                "builtins.input", return_value="y"
            ):
                cli.command_chat(chat_args, store, console)

        self.assertIn(
            "/api/workspaces/workspace-aaaaaaaaaaaaaaaa",
            MockControlPlane.deleted_paths,
        )
        self.assertIn(
            "/api/workspaces/workspace-aaaaaaaaaaaaaaaa/conversations/"
            "conversation-bbbbbbbbbbbbbbbb",
            MockControlPlane.deleted_paths,
        )

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

    def test_conversation_list_and_history_are_human_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = cli.SessionStore(Path(directory) / "state.json")
            store.save({"cookie": "aap_session=test-token", "baseUrl": self.base_url})
            output = io.StringIO()
            console = cli.Console(False, output)
            common = {"base_url": self.base_url, "instance_id": "agent-demo"}
            cli.command_conversations(SimpleNamespace(**common), store, console)
            cli.command_history(
                SimpleNamespace(
                    **common,
                    conversation_id="conversation-demo",
                    limit=20,
                    before=None,
                ),
                store,
                console,
            )
        rendered = output.getvalue()
        self.assertIn("conversation-demo", rendered)
        self.assertIn("演示会话", rendered)
        self.assertIn("你 > 旧问题", rendered)
        self.assertIn("Pi > 旧回答", rendered)


if __name__ == "__main__":
    unittest.main()
