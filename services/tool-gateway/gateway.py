#!/usr/bin/env python3
"""宿主侧模型 Tool Gateway。

microVM 只知道 TAP 地址和占位 API Key；真实 DeepSeek Key 保存在 Ubuntu 宿主的
受限文件中，由本进程读取并在转发请求时注入。Gateway 只开放经过白名单允许的
Chat Completions 路由，审计记录元数据而不记录 Authorization 或完整请求正文。

普通 JSON 响应仍有 8 MiB 上限；流式请求则以 HTTP chunked 编码边读边写 SSE，
避免在高权限 Gateway 中缓冲完整模型回答。审计只记录耗时、字节数和状态。
"""

import argparse
import json
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def append_audit(path: Path, event: dict) -> None:
    """追加一行紧凑 JSON 审计事件；调用方不得传入凭据或完整正文。"""
    event = {"timestamp": int(time.time()), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")


def main() -> None:
    """读取宿主凭据并启动绑定到指定 TAP 地址的并发 Gateway。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", required=True)
    parser.add_argument("--listen-port", required=True, type=int)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--audit-file", required=True, type=Path)
    parser.add_argument("--activity-file", type=Path)
    parser.add_argument("--tenant", default="smoke-tenant")
    args = parser.parse_args()

    credential = args.credential_file.read_text(encoding="utf-8").strip()
    if not credential:
        raise SystemExit("credential file is empty")

    class GatewayHandler(BaseHTTPRequestHandler):
        """执行路由限制、请求校验、凭据替换、上游转发和脱敏审计。"""

        server_version = "AgentToolGateway/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *args: object) -> None:
            # 禁用 BaseHTTPRequestHandler 的原始访问日志，避免未来误记敏感头。
            return

        def send_json(self, status: int, payload: dict) -> None:
            """返回 Gateway 自身产生的 JSON 错误。"""
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            """只代理 `/v1/chat/completions`，其他路径默认拒绝。"""
            if self.path != "/v1/chat/completions":
                append_audit(
                    args.audit_file,
                    {"method": "POST", "path": self.path, "status": 404},
                )
                self.send_json(404, {"error": "route_not_allowed"})
                return

            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = 0
            if content_length < 2 or content_length > 65536:
                append_audit(
                    args.audit_file,
                    {"method": "POST", "path": self.path, "status": 413},
                )
                self.send_json(413, {"error": "invalid_request_size"})
                return

            request_body = self.rfile.read(content_length)
            try:
                request_payload = json.loads(request_body)
            except json.JSONDecodeError:
                append_audit(
                    args.audit_file,
                    {"method": "POST", "path": self.path, "status": 400},
                )
                self.send_json(400, {"error": "invalid_json"})
                return
            if not isinstance(request_payload, dict):
                append_audit(
                    args.audit_file,
                    {"method": "POST", "path": self.path, "status": 400},
                )
                self.send_json(400, {"error": "invalid_request"})
                return

            # 不转发 guest 提供的 Authorization；始终使用宿主文件中的真实凭据。
            upstream_request = urllib.request.Request(
                f"{args.upstream}{self.path}",
                data=request_body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Content-Type": "application/json",
                },
            )

            started_at = time.monotonic()
            status = 502
            response_body = b'{"error":"upstream_unavailable"}'
            response_content_type = "application/json"
            first_byte_ms = None
            response_bytes = 0
            streaming = bool(request_payload.get("stream", False))
            streamed_response = False
            try:
                with urllib.request.urlopen(upstream_request, timeout=120) as response:
                    status = response.status
                    response_content_type = response.headers.get(
                        "Content-Type", "application/json"
                    )
                    if streaming:
                        # 不设置 Content-Length；逐块编码使 Pi 能在上游仍生成时消费 SSE。
                        self.send_response(status)
                        self.send_header("Content-Type", response_content_type)
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        streamed_response = True
                        while True:
                            # DeepSeek 的 Chat Completions 流使用 SSE：每条 data 记录以换行
                            # 结束。按行读取可在单条事件到达时立即转发；read1(8192)
                            # 可能把首个事件之后的多个 token 合并成一个较大块，页面就会
                            # 表现为“先出现一句，随后整段突然完成”。限制单行大小仍保留
                            # 对异常上游的内存边界，超长行会在下一轮继续转发。
                            chunk = response.readline(64 * 1024)
                            if not chunk:
                                break
                            if first_byte_ms is None:
                                first_byte_ms = int((time.monotonic() - started_at) * 1000)
                            response_bytes += len(chunk)
                            if response_bytes > 8 * 1024 * 1024:
                                raise RuntimeError("upstream streaming response exceeded 8 MiB")
                            self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                            self.wfile.write(chunk)
                            self.wfile.write(b"\r\n")
                            self.wfile.flush()
                            if args.activity_file is not None:
                                try:
                                    args.activity_file.touch()
                                except OSError:
                                    pass
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    else:
                        response_body = response.read(8 * 1024 * 1024 + 1)
                        response_bytes = len(response_body)
                        if response_bytes:
                            first_byte_ms = int((time.monotonic() - started_at) * 1000)
            except urllib.error.HTTPError as error:
                status = error.code
                response_content_type = error.headers.get(
                    "Content-Type", "application/json"
                )
                response_body = error.read(1024 * 1024 + 1)
                response_bytes = len(response_body)
            except (urllib.error.URLError, TimeoutError):
                pass
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                # 流式响应已经发送 HTTP 头后无法改写为 JSON 错误，只能结束连接并审计。
                status = 502
                self.close_connection = True

            # 限制响应体，防止异常上游无限占用高权限 Gateway 内存。
            if len(response_body) > 8 * 1024 * 1024:
                status = 502
                response_content_type = "application/json"
                response_body = b'{"error":"upstream_response_too_large"}'

            append_audit(
                args.audit_file,
                {
                    "credential_source": "host_file",
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "first_byte_ms": first_byte_ms,
                    "method": "POST",
                    "path": self.path,
                    "response_bytes": response_bytes,
                    "stream": streaming,
                    "status": status,
                    "tenant": args.tenant,
                },
            )
            if args.activity_file is not None:
                # 成功经过 Gateway 的模型活动可以延长实例生命周期。
                try:
                    args.activity_file.touch()
                except OSError:
                    pass
            if not streamed_response:
                self.send_response(status)
                self.send_header("Content-Type", response_content_type)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)

    server = ThreadingHTTPServer((args.listen_host, args.listen_port), GatewayHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
