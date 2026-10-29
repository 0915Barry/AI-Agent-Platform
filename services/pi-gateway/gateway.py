#!/usr/bin/env python3
"""宿主机侧 Pi RPC 客户端。

Pi 进程运行在 microVM 内，并通过 socat 暴露 TCP 端口。这个模块负责把平台任务
转换为 Pi RPC JSONL 命令，再把 Pi 的原生事件转换为控制面可保存的业务事件。
它不创建 microVM、不管理数据库，也不处理 HTTP 请求。

完整链路是：Control Plane 领取任务 -> PiGateway 建立 TCP 连接 -> Guest socat
把字节桥接到 ``pi --mode rpc`` 的 stdin/stdout -> PiGateway 解析返回的 JSONL，
再通过回调把标准化事件交还 Control Plane 保存。
"""

from __future__ import annotations

import json
import socket
import time
import uuid
from collections.abc import Callable
from typing import Any


class PiGatewayError(RuntimeError):
    """连接、协议或 Pi 执行失败。"""


TaskEventHandler = Callable[[str, dict[str, Any]], None]


class PiGateway:
    """通过 TCP 连接一个实例中的 Pi RPC，并执行一次平台任务。

    对象本身只保存超时配置，没有某个用户或实例的可变状态，因此可以被控制面
    复用。每次 execute() 都建立独立连接，任务之间不会共享 reader/writer。
    """

    MAX_RECORD_BYTES = 4 * 1024 * 1024

    def __init__(self, connect_timeout: float = 10, task_timeout: float = 240) -> None:
        self.connect_timeout = connect_timeout
        self.task_timeout = task_timeout

    @staticmethod
    def _assistant_text(message: Any) -> str | None:
        """从 Pi 的最终 assistant message 中提取文本内容。

        Pi 可能直接返回字符串，也可能返回由 text/tool 等内容块组成的数组；平台
        最终输出只拼接其中的文本块，工具执行过程由独立事件记录。
        """

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
        """只保留前端展示和审计需要的工具结果字段。

        原始工具结果可能包含大文件、内部结构或敏感字段，所以不整包进入数据库；
        这里只保存工具标识、错误状态和最多 2000 字符的文本预览。
        """

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
        """按 JSONL 协议发送一条记录，并立即刷新到 TCP 连接。"""

        # JSONL 不是一个大 JSON 数组，而是“一行一个 JSON 对象”。换行符既是记录
        # 边界，也是 Guest 中 Pi RPC 判断一条命令已经发送完整的依据。
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        writer.write(encoded + b"\n")
        writer.flush()

    def _connect(self, host: str, port: int) -> socket.socket:
        """在 Guest RPC 监听器短暂重启期间进行有界连接重试。"""

        deadline = time.monotonic() + self.connect_timeout
        last_error: OSError | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return socket.create_connection(
                    (host, port), timeout=min(0.5, remaining)
                )
            except OSError as error:
                last_error = error
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        raise PiGatewayError(
            f"cannot connect to Pi RPC at {host}:{port}: {last_error}"
        ) from last_error

    def execute(
        self,
        host: str,
        port: int,
        prompt: str,
        on_event: TaskEventHandler,
        workspace_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """执行一个 prompt，返回最终文本，并同步上报流式与工具事件。

        host/port 来自 InstanceRuntime 保存的 Guest RPC 端点；prompt 与
        workspace_id 与 session_id 来自会话记录；on_event 由 Control Plane 提供，
        用于持久化流式文本、工具调用和进度事件。

        协议顺序为：发送 prompt 命令 -> 等待同 id 的 response 确认 -> 持续接收
        agent/message/tool 事件 -> 收到 agent_settled 后返回最终文本。仅收到
        message_end 还不能视为完成，因为 Pi 后面仍可能重试或继续调用工具。

        每次调用建立一条独立 TCP 连接。Guest 中的 socat 会为该连接启动一个
        ``pi --mode rpc --no-session`` 进程，因此连接关闭后不会残留 Pi 子进程。
        """

        # command_id 用来把 Pi 的 response 与本次 prompt 对应起来；流式事件本身
        # 沿用同一条 TCP 连接按顺序到达，不需要为每个 token 再生成 ID。
        command_id = f"gateway-{uuid.uuid4().hex}"
        final_output: str | None = None
        prompt_accepted = False
        agent_settled = False
        # disposition 描述 Pi 如何处理命令：立即启动、进入 Pi 内部队列，或已同步
        # 处理完成。它不是平台 TaskStore 中 queued/running 状态的替代品。
        disposition: str | None = None

        connection = self._connect(host, port)

        with connection:
            connection.settimeout(self.task_timeout)
            # makefile() 在同一 TCP socket 上提供按行读写接口，便于严格按 JSONL
            # 边界读取；底层仍是同一条全双工 TCP 连接。
            reader = connection.makefile("rb")
            writer = connection.makefile("wb")
            try:
                command = {"id": command_id, "type": "prompt", "message": prompt}
                if workspace_id is not None:
                    # workspace 是平台传给 Guest 包装层的扩展字段，用于把 Pi 当前
                    # 工作目录限制到对应工作区；它不是模型提示词的一部分。
                    command["workspace"] = workspace_id
                if session_id is not None:
                    # Guest 包装层会把平台 conversation ID 转换为 Pi 的 --session-id，
                    # Pi 因而能从用户数据盘恢复同一原生 Session。
                    command["session"] = session_id
                self._send(writer, command)
                while True:
                    try:
                        line = reader.readline(self.MAX_RECORD_BYTES + 1)
                    except TimeoutError as error:
                        raise PiGatewayError(
                            f"Pi RPC task exceeded {self.task_timeout:g} seconds"
                        ) from error
                    if not line:
                        raise PiGatewayError("Pi RPC connection closed before agent_settled")
                    # 在 JSON 解码前限制单条记录大小，防止异常 Guest 用超大行占满
                    # Control Plane 内存。正常文本流会拆成多条较小的 delta 事件。
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
                        # response 只确认 Pi 接受了命令，不代表模型已经完成回答。
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
                        # 文本增量立即交给上层，使 CLI/SSE 可以边生成边显示，而不用
                        # 等待完整 assistant message。
                        update = record.get("assistantMessageEvent")
                        if isinstance(update, dict) and update.get("type") == "text_delta":
                            delta = update.get("delta")
                            if isinstance(delta, str) and delta:
                                on_event("text", {"delta": delta})
                    elif record_type == "message_end":
                        # 暂存最终消息，但仍继续读取到 agent_settled；工具调用、自动
                        # 重试或后续模型轮次可能发生在某个 message_end 之后。
                        text = self._assistant_text(record.get("message"))
                        if text is not None:
                            final_output = text
                    elif record_type == "tool_execution_start":
                        # 把 Pi 原生字段转换成平台统一事件名，Control Plane 无需理解
                        # Pi 的全部 RPC 结构即可保存并推送给不同客户端。
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
                        agent_settled = True
                        break
            except socket.timeout as error:
                raise PiGatewayError(
                    f"Pi RPC task exceeded {self.task_timeout:g} seconds"
                ) from error
            except OSError as error:
                raise PiGatewayError(f"Pi RPC transport failed: {error}") from error
            finally:
                # makefile() 创建了独立缓冲对象，需要显式关闭；外层 with 会随后关闭
                # socket，并使 Guest socat 结束本次 Pi RPC 子进程。
                writer.close()
                reader.close()

        if not agent_settled or final_output is None:
            raise PiGatewayError("Pi RPC ended without a settled result")

        # 连接关闭后，Guest 包装脚本会等待 Pi 退出并执行 sync，然后 socat 才重新
        # 进入监听。这里用一次立即关闭的探测连接确认该顺序已经完成，避免上层刚
        # 收到 completed 就停止 Firecracker，导致 Session 消息仍停留在页缓存。
        probe = self._connect(host, port)
        probe.close()
        return final_output

    def get_messages(
        self,
        host: str,
        port: int,
        workspace_id: str | None,
        session_id: str,
    ) -> list[dict[str, Any]]:
        """从数据盘中的 Pi 原生 Session 读取当前分支消息。

        Guest 包装层用首条记录中的 workspace/session 字段选择工作目录和
        ``--session-id``；Pi 启动后直接从持久化 Session 回复 get_messages。
        """

        command_id = f"gateway-{uuid.uuid4().hex}"
        command = {
            "id": command_id,
            "type": "get_messages",
            "session": session_id,
        }
        if workspace_id is not None:
            # 旧式实例级会话没有 workspace_id，Guest 此时使用默认 /workspace。
            command["workspace"] = workspace_id
        connection = self._connect(host, port)

        with connection:
            connection.settimeout(self.task_timeout)
            reader = connection.makefile("rb")
            writer = connection.makefile("wb")
            try:
                self._send(writer, command)
                while True:
                    line = reader.readline(self.MAX_RECORD_BYTES + 1)
                    if not line:
                        raise PiGatewayError("Pi RPC connection closed before get_messages response")
                    if len(line) > self.MAX_RECORD_BYTES:
                        raise PiGatewayError("Pi RPC record exceeds 4 MiB")
                    try:
                        record = json.loads(line.rstrip(b"\r\n").decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise PiGatewayError("Pi RPC returned invalid JSONL") from error
                    if not isinstance(record, dict):
                        continue
                    if record.get("type") != "response" or record.get("id") != command_id:
                        continue
                    if not record.get("success"):
                        raise PiGatewayError(str(record.get("error") or "Pi rejected get_messages"))
                    data = record.get("data")
                    messages = data.get("messages") if isinstance(data, dict) else None
                    if not isinstance(messages, list) or not all(
                        isinstance(message, dict) for message in messages
                    ):
                        raise PiGatewayError("Pi get_messages returned an invalid message list")
                    return messages
            except socket.timeout as error:
                raise PiGatewayError(
                    f"Pi RPC history request exceeded {self.task_timeout:g} seconds"
                ) from error
            except OSError as error:
                raise PiGatewayError(f"Pi RPC transport failed: {error}") from error
            finally:
                writer.close()
                reader.close()
