#!/usr/bin/env python3
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def append_audit(path: Path, event: dict) -> None:
    event = {"timestamp": int(time.time()), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", required=True, type=int)
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--audit-file", required=True, type=Path)
    args = parser.parse_args()

    expected_credential = args.credential_file.read_text(encoding="utf-8").strip()

    class UpstreamHandler(BaseHTTPRequestHandler):
        server_version = "MockModelUpstream/0.1"

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
            self.rfile.read(min(content_length, 65536))
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
