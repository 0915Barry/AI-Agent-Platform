#!/usr/bin/env python3
"""Pi Gateway 的本地协议测试，不依赖 KVM、模型或 microVM。"""

import importlib.util
import json
import socket
import threading
import unittest
from pathlib import Path


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
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.host, self.port = self.listener.getsockname()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
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
            server.host, server.port, "测试", lambda kind, payload: events.append((kind, payload))
        )
        server.wait()

        self.assertEqual(server.command["type"], "prompt")
        self.assertEqual(server.command["message"], "测试")
        self.assertEqual(output, "你好，完成")
        self.assertEqual([kind for kind, _ in events], ["progress", "text", "tool_call", "tool_result"])

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


if __name__ == "__main__":
    unittest.main()
