#!/usr/bin/env python3
"""实例专属的最小 Streamable HTTP MCP Gateway。

该进程运行在 Ubuntu 宿主机，而不是 microVM 内。guest 只能通过实例 TAP 网络和
一次性 Bearer token 访问；真正的数据文件始终由宿主侧低权限用户读取。当前只暴露
一个只读验收工具，后续接数据库或企业 API 时继续沿用相同的白名单、超时与审计边界。

协议实现刻意保持很小，只覆盖 Pi Agent 1.0.0 实际需要的 initialize、tools/list、
tools/call、initialized notification、GET 405 降级以及 DELETE 会话关闭。
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2025-11-25"
TOOL_NAME = "get_verification_record"
RECORD_ID = "m19-verification"
MAX_BODY_BYTES = 64 * 1024


class McpGateway:
    """保存实例认证信息、短会话和工具调用审计。"""

    def __init__(self, token_file: Path, data_file: Path, audit_file: Path, instance_id: str) -> None:
        self.token = token_file.read_text(encoding="utf-8").strip()
        self.data_file = data_file
        self.audit_file = audit_file
        self.instance_id = instance_id
        self._sessions: set[str] = set()
        self._lock = threading.Lock()
        if not self.token:
            raise ValueError("MCP token file is empty")

    def authorized(self, authorization: str | None) -> bool:
        expected = f"Bearer {self.token}"
        return authorization is not None and hmac.compare_digest(authorization, expected)

    def create_session(self) -> str:
        session_id = secrets.token_urlsafe(24)
        with self._lock:
            self._sessions.add(session_id)
        return session_id

    def has_session(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self._lock:
            return session_id in self._sessions

    def close_session(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            self._sessions.discard(session_id)

    def audit(self, event: str, **details: Any) -> None:
        """只记录结构化元数据，不记录 Authorization 或工具返回正文。"""

        entry = {
            "timestamp": int(time.time()),
            "instance": self.instance_id,
            "event": event,
            **details,
        }
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            with self.audit_file.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def dispatch(self, message: dict[str, Any], session_id: str | None) -> tuple[dict | None, str | None]:
        """处理单条 JSON-RPC 消息；第二个返回值是新建的会话 ID。"""

        request_id = message.get("id")
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return self.error(request_id, -32600, "Invalid Request"), None

        if method == "initialize":
            params = message.get("params")
            if not isinstance(params, dict):
                return self.error(request_id, -32602, "Invalid initialize parameters"), None
            requested = params.get("protocolVersion")
            supported = {PROTOCOL_VERSION, "2025-06-18", "2025-03-26", "2024-11-05"}
            if requested not in supported:
                return self.error(request_id, -32602, "Unsupported MCP protocol version"), None
            created_session = self.create_session()
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ai-agent-platform-mcp-gateway", "version": "0.1.0"},
                "instructions": "Use only the reviewed read-only verification tool when requested.",
            }
            self.audit("initialized", protocol=PROTOCOL_VERSION)
            return self.result(request_id, result), created_session

        if not self.has_session(session_id):
            return self.error(request_id, -32001, "Unknown or expired MCP session"), None

        if method == "notifications/initialized":
            return None, None
        if method == "ping":
            return self.result(request_id, {}), None
        if method == "tools/list":
            tool = {
                "name": TOOL_NAME,
                "title": "读取 M19 验收记录",
                "description": "从宿主侧受保护数据文件读取指定的只读验收记录。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "recordId": {
                            "type": "string",
                            "description": f"固定记录 ID：{RECORD_ID}",
                        }
                    },
                    "required": ["recordId"],
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            }
            return self.result(request_id, {"tools": [tool]}), None
        if method == "tools/call":
            return self.call_tool(request_id, message.get("params")), None
        if method == "resources/list":
            return self.result(request_id, {"resources": []}), None
        if method == "resources/templates/list":
            return self.result(request_id, {"resourceTemplates": []}), None
        return self.error(request_id, -32601, f"Method not found: {method}"), None

    def call_tool(self, request_id: Any, params: Any) -> dict:
        if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
            return self.error(request_id, -32602, "Unknown MCP tool")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict) or arguments.get("recordId") != RECORD_ID:
            self.audit("tool_call", tool=TOOL_NAME, status="denied", reason="invalid_record_id")
            return self.result(
                request_id,
                {"content": [{"type": "text", "text": "record not found"}], "isError": True},
            )
        value = self.data_file.read_text(encoding="utf-8").strip()
        if not value or len(value.encode("utf-8")) > 4096:
            self.audit("tool_call", tool=TOOL_NAME, status="error", reason="invalid_host_data")
            return self.error(request_id, -32603, "Host verification data is unavailable")
        self.audit("tool_call", tool=TOOL_NAME, status="allowed", record=RECORD_ID)
        return self.result(
            request_id,
            {
                "content": [{"type": "text", "text": f"M19_MCP_OK:{value}"}],
                "structuredContent": {"recordId": RECORD_ID, "value": value},
                "isError": False,
            },
        )

    @staticmethod
    def result(request_id: Any, value: Any) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": value}

    @staticmethod
    def error(request_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class Handler(BaseHTTPRequestHandler):
    """严格限制路径、媒体类型、请求大小与认证的 HTTP 适配层。"""

    server_version = "AgentMcpGateway/0.1"

    @property
    def gateway(self) -> McpGateway:
        return self.server.gateway  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        # Pi 会尝试建立服务器主动消息流。当前服务器没有通知，因此用 405 让客户端
        # 按其内置逻辑降级为仅 POST，而不是维持一个无意义的 SSE 长连接。
        self.send_response(405)
        self.send_header("Allow", "POST, DELETE")
        self.end_headers()

    def do_DELETE(self) -> None:
        if not self.authenticate() or self.path != "/mcp":
            return
        self.gateway.close_session(self.headers.get("Mcp-Session-Id"))
        self.send_response(204)
        self.end_headers()

    def do_POST(self) -> None:
        if not self.authenticate() or self.path != "/mcp":
            return
        if self.headers.get_content_type() != "application/json":
            self.send_error(415, "Content-Type must be application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_error(413, "Invalid request size")
            return
        try:
            message = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(McpGateway.error(None, -32700, "Parse error"), 400)
            return
        if not isinstance(message, dict):
            self.send_json(McpGateway.error(None, -32600, "Invalid Request"), 400)
            return
        response, created_session = self.gateway.dispatch(
            message, self.headers.get("Mcp-Session-Id")
        )
        if response is None:
            self.send_response(202)
            self.end_headers()
            return
        self.send_json(response, 200, created_session)

    def authenticate(self) -> bool:
        if self.path != "/mcp":
            self.send_error(404)
            return False
        if not self.gateway.authorized(self.headers.get("Authorization")):
            self.gateway.audit("authentication_failed", remote=self.client_address[0])
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="mcp-gateway"')
            self.end_headers()
            return False
        return True

    def send_json(self, payload: dict, status: int, session_id: str | None = None) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        # HTTP access log 由外层 sidecar 文件接收；认证头从不写入日志。
        print(f"{self.client_address[0]} {format % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Host-side reviewed MCP Gateway")
    parser.add_argument("--listen-host", required=True)
    parser.add_argument("--listen-port", type=int, default=18084)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--data-file", type=Path, required=True)
    parser.add_argument("--audit-file", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    args = parser.parse_args()

    for protected in (args.token_file, args.data_file):
        if not protected.is_file():
            raise SystemExit(f"required protected file is missing: {protected}")
        if os.stat(protected).st_mode & 0o077:
            raise SystemExit(f"protected file permissions are too broad: {protected}")
    args.audit_file.parent.mkdir(parents=True, exist_ok=True)
    gateway = McpGateway(args.token_file, args.data_file, args.audit_file, args.instance_id)
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), Handler)
    server.gateway = gateway  # type: ignore[attr-defined]
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
