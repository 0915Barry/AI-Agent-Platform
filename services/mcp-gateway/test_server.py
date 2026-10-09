#!/usr/bin/env python3
"""M19 MCP Gateway 的本机协议、认证与审计测试。"""

import importlib.util
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path


SERVER_PATH = Path(__file__).resolve().with_name("server.py")
SPEC = importlib.util.spec_from_file_location("mcp_gateway_server", SERVER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load MCP Gateway")
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class McpGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.token = root / "token"
        self.data = root / "data"
        self.audit = root / "audit.jsonl"
        self.token.write_text("guest-token\n", encoding="utf-8")
        self.data.write_text("host-only-value\n", encoding="utf-8")
        self.gateway = SERVER.McpGateway(self.token, self.data, self.audit, "test-instance")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SERVER.Handler)
        self.server.gateway = self.gateway
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)

    def tearDown(self) -> None:
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def post(self, message: dict, session: str | None = None, token: str = "guest-token"):
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if session:
            headers["Mcp-Session-Id"] = session
        self.connection.request("POST", "/mcp", json.dumps(message), headers)
        response = self.connection.getresponse()
        body = response.read()
        return response, json.loads(body) if body else None

    def test_initialize_list_call_and_redacted_audit(self) -> None:
        response, payload = self.post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}},
        })
        self.assertEqual(response.status, 200)
        session = response.getheader("Mcp-Session-Id")
        self.assertTrue(session)
        self.assertEqual(payload["result"]["protocolVersion"], "2025-11-25")

        response, payload = self.post(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["result"]["tools"][0]["name"], "get_verification_record")

        response, payload = self.post({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get_verification_record",
                       "arguments": {"recordId": "m19-verification"}},
        }, session)
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["result"]["content"][0]["text"], "M19_MCP_OK:host-only-value")
        audit = self.audit.read_text(encoding="utf-8")
        self.assertIn('"status":"allowed"', audit)
        self.assertNotIn("guest-token", audit)
        self.assertNotIn("host-only-value", audit)

    def test_authentication_and_unknown_session_are_rejected(self) -> None:
        response, _ = self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token="wrong"
        )
        self.assertEqual(response.status, 401)
        response, payload = self.post(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "unknown"
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["error"]["code"], -32001)


if __name__ == "__main__":
    unittest.main()
