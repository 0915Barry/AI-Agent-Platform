#!/usr/bin/env python3
"""Pi Gateway 的本地协议测试，不依赖 KVM、模型或 microVM。"""

import importlib.util
import json
import socket
import threading
import unittest
from pathlib import Path
from unittest import mock


GATEWAY_PATH = Path(__file__).resolve().with_name("gateway.py")
SPEC = importlib.util.spec_from_file_location("pi_gateway", GATEWAY_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load Pi Gateway")
GATEWAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATEWAY)


class FakePiServer:
    """接受一条 prompt，并返回一组确定的 Pi RPC 记录。"""

    def __init__(self, records: list[dict]) -> None:
        self.records = records
        self.command: dict | None = None
        self.flush_probe_received = False
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.host, self.port = self.listener.getsockname()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        expects_flush_probe = any(record.get("type") == "agent_settled" for record in self.records)
        connection, _ = self.listener.accept()
        with connection:
            reader = connection.makefile("rb")
            writer = connection.makefile("wb")
            self.command = json.loads(reader.readline())
            command_id = self.command["id"]
            for source in self.records:
                record = json.loads(json.dumps(source))
                if record.pop("useCommandId", False):
                    record["id"] = command_id
                writer.write(json.dumps(record).encode("utf-8") + b"\n")
                writer.flush()
            writer.close()
            reader.close()
        if expects_flush_probe:
            probe, _ = self.listener.accept()
            probe.close()
            self.flush_probe_received = True
        self.listener.close()

    def start(self) -> None:
        self.thread.start()

    def wait(self) -> None:
        self.thread.join(timeout=2)


class PiGatewayTests(unittest.TestCase):
    def test_prompt_stream_and_tool_events_are_normalized(self) -> None:
        server = FakePiServer(
            [
                {"type": "agent_start"},
                {
                    "type": "response",
                    "command": "prompt",
                    "success": True,
                    "data": {"disposition": "started"},
                    "useCommandId": True,
                },
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "你好"},
                },
                {
                    "type": "tool_execution_start",
                    "toolCallId": "call-1",
                    "toolName": "read",
                    "args": {"path": "/workspace/a.txt"},
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": "call-1",
                    "toolName": "read",
                    "isError": False,
                    "result": {"content": [{"type": "text", "text": "A"}]},
                },
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "你好，完成"}],
                    },
                },
                {"type": "agent_settled", "aborted": False},
            ]
        )
        server.start()
        events: list[tuple[str, dict]] = []
        output = GATEWAY.PiGateway(task_timeout=2).execute(
            server.host,
            server.port,
            "测试",
            lambda kind, payload: events.append((kind, payload)),
            "workspace-0123456789abcdef",
            "conversation-0123456789abcdef",
        )
        server.wait()

        self.assertEqual(server.command["type"], "prompt")
        self.assertEqual(server.command["message"], "测试")
        self.assertEqual(server.command["workspace"], "workspace-0123456789abcdef")
        self.assertEqual(server.command["session"], "conversation-0123456789abcdef")
        self.assertEqual(output, "你好，完成")
        self.assertEqual([kind for kind, _ in events], ["progress", "text", "tool_call", "tool_result"])
        self.assertTrue(server.flush_probe_received)

    def test_rejected_prompt_raises_protocol_error(self) -> None:
        server = FakePiServer(
            [
                {
                    "type": "response",
                    "command": "prompt",
                    "success": False,
                    "error": "busy",
                    "useCommandId": True,
                }
            ]
        )
        server.start()
        with self.assertRaisesRegex(GATEWAY.PiGatewayError, "busy"):
            GATEWAY.PiGateway(task_timeout=2).execute(
                server.host, server.port, "测试", lambda _kind, _payload: None
            )
        server.wait()

    def test_get_messages_selects_persistent_session(self) -> None:
        server = FakePiServer(
            [
                {
                    "type": "response",
                    "command": "get_messages",
                    "success": True,
                    "data": {"messages": [{"role": "user", "content": "历史消息"}]},
                    "useCommandId": True,
                }
            ]
        )
        server.start()
        messages = GATEWAY.PiGateway(task_timeout=2).get_messages(
            server.host,
            server.port,
            "workspace-0123456789abcdef",
            "conversation-0123456789abcdef",
        )
        server.wait()

        self.assertEqual(messages[0]["content"], "历史消息")
        self.assertEqual(server.command["type"], "get_messages")
        self.assertEqual(server.command["workspace"], "workspace-0123456789abcdef")
        self.assertEqual(server.command["session"], "conversation-0123456789abcdef")

    def test_get_messages_uses_default_workspace_when_unscoped(self) -> None:
        """旧式实例级会话不应向 Pi 发送空的 workspace 扩展字段。"""

        server = FakePiServer(
            [
                {
                    "type": "response",
                    "command": "get_messages",
                    "success": True,
                    "data": {"messages": []},
                    "useCommandId": True,
                }
            ]
        )
        server.start()
        GATEWAY.PiGateway(task_timeout=2).get_messages(
            server.host,
            server.port,
            None,
            "conversation-0123456789abcdef",
        )
        server.wait()

        self.assertNotIn("workspace", server.command)
        self.assertEqual(server.command["session"], "conversation-0123456789abcdef")

    def test_connection_retries_during_guest_listener_restart(self) -> None:
        """历史连接关闭后 socat 重启监听的短暂空窗不应导致任务失败。"""

        server = FakePiServer(
            [
                {
                    "type": "response",
                    "command": "get_messages",
                    "success": True,
                    "data": {"messages": []},
                    "useCommandId": True,
                }
            ]
        )
        server.start()
        real_create_connection = socket.create_connection
        attempts = 0

        def temporarily_refused(address, timeout=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionRefusedError(111, "Connection refused")
            return real_create_connection(address, timeout=timeout)

        with mock.patch.object(
            GATEWAY.socket, "create_connection", side_effect=temporarily_refused
        ):
            GATEWAY.PiGateway(connect_timeout=1, task_timeout=2).get_messages(
                server.host,
                server.port,
                None,
                "conversation-0123456789abcdef",
            )
        server.wait()

        self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()
