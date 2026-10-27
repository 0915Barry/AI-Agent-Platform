#!/usr/bin/env python3
"""宿主机侧 Pi RPC 客户端。

Pi 进程运行在 microVM 内，并通过 socat 暴露 TCP 端口。这个模块负责把平台任务
转换为 Pi RPC JSONL 命令，再把 Pi 的原生事件转换为控制面可保存的业务事件。
它不创建 microVM、不管理数据库，也不处理 HTTP 请求。
"""

from __future__ import annotations

import json
import socket
import uuid
from collections.abc import Callable
from typing import Any


class PiGatewayError(RuntimeError):
    """连接、协议或 Pi 执行失败。"""


TaskEventHandler = Callable[[str, dict[str, Any]], None]


class PiGateway:
    """通过 TCP 连接一个实例中的 Pi RPC，并执行一次平台任务。"""

    MAX_RECORD_BYTES = 4 * 1024 * 1024

    def __init__(self, connect_timeout: float = 10, task_timeout: float = 240) -> None:
        self.connect_timeout = connect_timeout
        self.task_timeout = task_timeout

    @staticmethod
    def _assistant_text(message: Any) -> str | None:
        """从 Pi 的最终 assistant message 中提取文本内容。"""

        if not isinstance(message, dict) or message.get("role") != "assistant":
            return None
        content = message.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return None
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        return "".join(parts)

    @staticmethod
    def _safe_tool_result(record: dict[str, Any]) -> dict[str, Any]:
        """只保留前端展示和审计需要的工具结果字段。"""

        result: dict[str, Any] = {
            "toolCallId": record.get("toolCallId"),
            "toolName": record.get("toolName"),
            "isError": bool(record.get("isError", False)),
        }
        tool_result = record.get("result")
        if isinstance(tool_result, dict):
            content = tool_result.get("content")
            if isinstance(content, list):
                text_parts = [
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict)
                    and item.get("type") == "text"
                    and isinstance(item.get("text"), str)
                ]
                preview = "\n".join(text_parts)
                result["preview"] = preview[:2000]
                result["truncated"] = len(preview) > 2000
        return result

    @staticmethod
    def _send(writer: Any, record: dict[str, Any]) -> None:
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        writer.write(encoded + b"\n")
        writer.flush()

    def execute(
        self,
        host: str,
        port: int,
        prompt: str,
        on_event: TaskEventHandler,
    ) -> str:
        """执行一个 prompt，返回最终文本，并同步上报流式与工具事件。

        每次调用建立一条独立 TCP 连接。Guest 中的 socat 会为该连接启动一个
        ``pi --mode rpc --no-session`` 进程，因此连接关闭后不会残留 Pi 子进程。
        """

        command_id = f"gateway-{uuid.uuid4().hex}"
        final_output: str | None = None
        prompt_accepted = False
        disposition: str | None = None

        try:
            connection = socket.create_connection((host, port), timeout=self.connect_timeout)
        except OSError as error:
            raise PiGatewayError(f"cannot connect to Pi RPC at {host}:{port}: {error}") from error

        with connection:
            connection.settimeout(self.task_timeout)
            reader = connection.makefile("rb")
            writer = connection.makefile("wb")
            try:
                self._send(
                    writer,
                    {"id": command_id, "type": "prompt", "message": prompt},
                )
                while True:
                    try:
                        line = reader.readline(self.MAX_RECORD_BYTES + 1)
                    except TimeoutError as error:
                        raise PiGatewayError(
                            f"Pi RPC task exceeded {self.task_timeout:g} seconds"
                        ) from error
                    if not line:
                        raise PiGatewayError("Pi RPC connection closed before agent_settled")
                    if len(line) > self.MAX_RECORD_BYTES:
                        raise PiGatewayError("Pi RPC record exceeds 4 MiB")
                    try:
                        record = json.loads(line.rstrip(b"\r\n").decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise PiGatewayError("Pi RPC returned invalid JSONL") from error
                    if not isinstance(record, dict):
                        raise PiGatewayError("Pi RPC record must be a JSON object")

                    record_type = record.get("type")
                    if record_type == "response" and record.get("id") == command_id:
                        if not record.get("success"):
                            raise PiGatewayError(str(record.get("error") or "Pi rejected prompt"))
                        data = record.get("data")
                        disposition = data.get("disposition") if isinstance(data, dict) else None
                        if disposition not in {"started", "queued", "handled"}:
                            raise PiGatewayError(f"invalid Pi prompt disposition: {disposition!r}")
                        prompt_accepted = True
                        if disposition == "handled":
                            return final_output or ""
                        continue

                    if record_type == "agent_start":
                        on_event("progress", {"stage": "model_started"})
                    elif record_type == "message_update":
                        update = record.get("assistantMessageEvent")
                        if isinstance(update, dict) and update.get("type") == "text_delta":
                            delta = update.get("delta")
                            if isinstance(delta, str) and delta:
                                on_event("text", {"delta": delta})
                    elif record_type == "message_end":
                        text = self._assistant_text(record.get("message"))
                        if text is not None:
                            final_output = text
                    elif record_type == "tool_execution_start":
                        on_event(
                            "tool_call",
                            {
                                "toolCallId": record.get("toolCallId"),
                                "toolName": record.get("toolName"),
                                "arguments": record.get("args"),
                            },
                        )
                    elif record_type == "tool_execution_end":
                        on_event("tool_result", self._safe_tool_result(record))
                    elif record_type == "auto_retry_start":
                        on_event(
                            "progress",
                            {
                                "stage": "retrying",
                                "attempt": record.get("attempt"),
                                "delayMs": record.get("delayMs"),
                            },
                        )
                    elif record_type == "agent_settled":
                        if not prompt_accepted:
                            raise PiGatewayError("Pi settled before acknowledging the prompt")
                        if record.get("aborted"):
                            raise PiGatewayError("Pi task was aborted")
                        if final_output is None:
                            raise PiGatewayError("Pi completed without an assistant message")
                        return final_output
            except socket.timeout as error:
                raise PiGatewayError(
                    f"Pi RPC task exceeded {self.task_timeout:g} seconds"
                ) from error
            except OSError as error:
                raise PiGatewayError(f"Pi RPC transport failed: {error}") from error
            finally:
                writer.close()
                reader.close()
