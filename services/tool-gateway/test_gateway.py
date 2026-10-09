#!/usr/bin/env python3
"""Tool Gateway 的本机回归测试；不需要 KVM，也不会访问真实 DeepSeek。"""

import http.client
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


SERVICE_DIR = Path(__file__).resolve().parent


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for_port(port: int) -> None:
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.02)
    raise RuntimeError(f"service did not bind port {port}")


class GatewayStreamingTests(unittest.TestCase):
    def test_streaming_response_is_forwarded_and_audited(self) -> None:
        """模拟 SSE 必须完整穿过 chunked Gateway，且审计中不能出现凭据。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            credential = root / "credential"
            credential.write_text("host-secret", encoding="utf-8")
            upstream_audit = root / "upstream.jsonl"
            gateway_audit = root / "gateway.jsonl"
            upstream_audit.touch()
            gateway_audit.touch()
            upstream_port = free_port()
            gateway_port = free_port()
            upstream = subprocess.Popen(
                [
                    sys.executable,
                    str(SERVICE_DIR / "mock_upstream.py"),
                    "--listen-port",
                    str(upstream_port),
                    "--credential-file",
                    str(credential),
                    "--audit-file",
                    str(upstream_audit),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            gateway = subprocess.Popen(
                [
                    sys.executable,
                    str(SERVICE_DIR / "gateway.py"),
                    "--listen-host",
                    "127.0.0.1",
                    "--listen-port",
                    str(gateway_port),
                    "--upstream",
                    f"http://127.0.0.1:{upstream_port}",
                    "--credential-file",
                    str(credential),
                    "--audit-file",
                    str(gateway_audit),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                wait_for_port(upstream_port)
                wait_for_port(gateway_port)
                payload = json.dumps(
                    {"model": "mock", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
                )
                connection = http.client.HTTPConnection("127.0.0.1", gateway_port, timeout=3)
                connection.request(
                    "POST",
                    "/v1/chat/completions",
                    body=payload,
                    headers={
                        "Authorization": "Bearer guest-placeholder",
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                body = response.read().decode("utf-8")
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertIn('"content":"gateway-"', body)
                self.assertIn('"content":"stream-"', body)
                self.assertIn('"content":"ok"', body)
                self.assertIn("data: [DONE]", body)

                audit = ""
                for _ in range(50):
                    audit = gateway_audit.read_text(encoding="utf-8")
                    if audit:
                        break
                    time.sleep(0.01)
                self.assertIn('"stream":true', audit)
                self.assertIn('"credential_source":"host_file"', audit)
                self.assertNotIn("host-secret", audit)
                self.assertIn('"auth_valid":true', upstream_audit.read_text(encoding="utf-8"))
            finally:
                for process in (gateway, upstream):
                    process.terminate()
                for process in (gateway, upstream):
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
