#!/usr/bin/env python3
"""M7 使用的本地模拟模型上游。

它不调用任何真实供应商，只验证 Gateway 是否用宿主凭据替换了 microVM 的占位
凭据，并把验证结果写入独立审计文件。此服务仅用于 smoke test，不能用于生产。
"""

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def append_audit(path: Path, event: dict) -> None:
    """写入模拟上游观察到的认证结果，不保存凭据本身。"""
    event = {"timestamp": int(time.time()), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")


def main() -> None:
    """启动仅供 Gateway 回归测试访问的 HTTP 服务。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", required=True, type=int)
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--audit-file", required=True, type=Path)
    args = parser.parse_args()

    expected_credential = args.credential_file.read_text(encoding="utf-8").strip()

    class UpstreamHandler(BaseHTTPRequestHandler):
        """检查 Authorization 是否等于预期宿主凭据并返回固定模型响应。"""

        server_version = "MockModelUpstream/0.1"

        def log_message(self, _format: str, *args: object) -> None:
            # 避免标准访问日志意外扩大测试数据的记录范围。
            return

        def send_json(self, status: int, payload: dict) -> None:
            """发送固定结构的模拟 JSON 响应。"""
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            """验证注入凭据并消费有限大小的请求体。"""
            authorization = self.headers.get("Authorization", "")
            auth_valid = authorization == f"Bearer {expected_credential}"
            append_audit(
                args.audit_file,
                {
                    "auth_valid": auth_valid,
                    "method": "POST",
                    "path": self.path,
                },
            )
            if not auth_valid:
                self.send_json(401, {"error": "invalid_upstream_credential"})
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            request_body = self.rfile.read(min(content_length, 65536))
            try:
                request_payload = json.loads(request_body)
            except json.JSONDecodeError:
                request_payload = {}
            if request_payload.get("stream") is True:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for delta in ("gateway-", "stream-", "ok"):
                    payload = {
                        "id": "gateway-stream-response",
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {"content": delta}}],
                    }
                    self.wfile.write(f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.03)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            self.send_json(
                200,
                {
                    "credential_valid": True,
                    "id": "gateway-smoke-response",
                    "object": "chat.completion",
                    "provider": "mock-upstream",
                },
            )

    server = ThreadingHTTPServer((args.listen_host, args.listen_port), UpstreamHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
