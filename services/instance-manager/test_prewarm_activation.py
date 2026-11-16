#!/usr/bin/env python3
"""预热激活协议客户端单元测试，不创建真实网络连接。"""

import unittest
from unittest.mock import patch

from prewarm_activation import ActivationClient, ActivationError, BoundReady


class FakeSocket:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent = b""
        self.timeout = None

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def sendall(self, payload: bytes) -> None:
        self.sent += payload

    def recv(self, _size: int) -> bytes:
        response, self.response = self.response, b""
        return response


class ActivationClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ActivationClient(3)

    def request(self, response: bytes):
        fake = FakeSocket(response)
        patcher = patch("prewarm_activation.socket.create_connection", return_value=fake)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return fake, mocked

    def test_probe_warm(self) -> None:
        fake, mocked = self.request(b"PROBE_READY state=warm\n")
        self.assertEqual(self.client.probe("172.29.0.2", 8081, "token-1"), "warm")
        self.assertEqual(fake.sent, b"PROBE_V1 token-1\n")
        mocked.assert_called_once_with(("172.29.0.2", 8081), timeout=3)

    def test_activate_parses_bound_ready(self) -> None:
        self.request(
            b"BOUND_READY activation_id=act-1 rpc_address=172.29.0.2:8080\n"
        )
        result = self.client.activate(
            "172.29.0.2",
            8081,
            "token-1",
            "act-1",
            "9580747b-07d7-457c-b70e-b14d4a9e8ce2",
        )
        self.assertEqual(result, BoundReady("act-1", "172.29.0.2", 8080))

    def test_commit_requires_matching_response(self) -> None:
        fake, _ = self.request(b"COMMITTED activation_id=act-1\n")
        self.client.commit("172.29.0.2", 8081, "token-1", "act-1")
        self.assertEqual(fake.sent, b"COMMIT_V1 token-1 act-1\n")

    def test_rejection_does_not_expose_token(self) -> None:
        self.request(b"ACTIVATE_REJECTED reason=bad_token\n")
        with self.assertRaisesRegex(ActivationError, "activation rejected: bad_token") as caught:
            self.client.probe("172.29.0.2", 8081, "secret-token")
        self.assertNotIn("secret-token", str(caught.exception))

    def test_wrong_activation_id_is_rejected(self) -> None:
        self.request(
            b"BOUND_READY activation_id=other rpc_address=172.29.0.2:8080\n"
        )
        with self.assertRaisesRegex(ActivationError, "unexpected ACTIVATE_V1"):
            self.client.activate(
                "172.29.0.2",
                8081,
                "token-1",
                "act-1",
                "9580747b-07d7-457c-b70e-b14d4a9e8ce2",
            )

    def test_malformed_or_oversized_response_is_rejected(self) -> None:
        self.request(b"not-a-response\n")
        with self.assertRaisesRegex(ActivationError, "unexpected PROBE_V1"):
            self.client.probe("172.29.0.2", 8081, "token-1")

        self.request(b"x" * 4097)
        with self.assertRaisesRegex(ActivationError, "size limit"):
            self.client.probe("172.29.0.2", 8081, "token-1")

    def test_invalid_request_fields_are_rejected_before_connect(self) -> None:
        with patch("prewarm_activation.socket.create_connection") as connect:
            with self.assertRaisesRegex(ActivationError, "invalid activation ID"):
                self.client.activate(
                    "172.29.0.2",
                    8081,
                    "token-1",
                    "bad id",
                    "9580747b-07d7-457c-b70e-b14d4a9e8ce2",
                )
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
