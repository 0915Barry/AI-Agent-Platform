#!/usr/bin/env python3
import argparse
import json
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def append_audit(path: Path, event: dict) -> None:
    event = {"timestamp": int(time.time()), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")


def main() -> None:
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
        server_version = "AgentToolGateway/0.1"

        def log_message(self, _format: str, *args: object) -> None:
            return

        def send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
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

            upstream_request = urllib.request.Request(
                f"{args.upstream}{self.path}",
                data=request_body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Content-Type": "application/json",
                },
            )

            status = 502
            response_body = b'{"error":"upstream_unavailable"}'
            response_content_type = "application/json"
            try:
                with urllib.request.urlopen(upstream_request, timeout=120) as response:
                    status = response.status
                    response_content_type = response.headers.get(
                        "Content-Type", "application/json"
                    )
                    response_body = response.read(8 * 1024 * 1024 + 1)
            except urllib.error.HTTPError as error:
                status = error.code
                response_content_type = error.headers.get(
                    "Content-Type", "application/json"
                )
                response_body = error.read(1024 * 1024 + 1)
            except (urllib.error.URLError, TimeoutError):
                pass

            if len(response_body) > 8 * 1024 * 1024:
                status = 502
                response_content_type = "application/json"
                response_body = b'{"error":"upstream_response_too_large"}'

            append_audit(
                args.audit_file,
                {
                    "credential_source": "host_file",
                    "method": "POST",
                    "path": self.path,
                    "stream": bool(request_payload.get("stream", False)),
                    "status": status,
                    "tenant": args.tenant,
                },
            )
            if args.activity_file is not None:
                try:
                    args.activity_file.touch()
                except OSError:
                    pass
            self.send_response(status)
            self.send_header("Content-Type", response_content_type)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

    server = ThreadingHTTPServer((args.listen_host, args.listen_port), GatewayHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
