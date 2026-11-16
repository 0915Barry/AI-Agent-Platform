#!/usr/bin/env python3
"""Host 到预热 Guest 的固定激活协议客户端。

协议只允许 ``PROBE_V1``、``ACTIVATE_V1`` 和 ``COMMIT_V1`` 三类单行请求。
本模块不负责状态机和 Firecracker API；它只建立一次 TCP 连接、发送一行、
读取一行并严格校验响应，避免 Runtime 各调用点自行拼接和宽松解析协议。
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass

MAX_RESPONSE_BYTES = 4096
SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class ActivationError(RuntimeError):
    """连接失败、响应格式错误或 Guest 明确拒绝激活。"""


@dataclass(frozen=True)
class BoundReady:
    activation_id: str
    rpc_host: str
    rpc_port: int


class ActivationClient:
    def __init__(self, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("activation timeout must be positive")
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _safe_id(name: str, value: str) -> str:
        if not SAFE_ID.fullmatch(value):
            raise ActivationError(f"invalid {name}")
        return value

    @staticmethod
    def _safe_uuid(value: str) -> str:
        if not UUID.fullmatch(value):
            raise ActivationError("invalid volume UUID")
        return value.lower()

    def _request(self, host: str, port: int, line: str) -> str:
        try:
            ipaddress.ip_address(host)
        except ValueError as error:
            raise ActivationError("activation host must be an IP address") from error
        if not 1 <= port <= 65535:
            raise ActivationError("invalid activation port")
        if "\n" in line or "\r" in line:
            raise ActivationError("activation request must be one line")

        try:
            with socket.create_connection(
                (host, port), timeout=self.timeout_seconds
            ) as connection:
                connection.settimeout(self.timeout_seconds)
                connection.sendall(line.encode("utf-8") + b"\n")
                response = bytearray()
                while len(response) <= MAX_RESPONSE_BYTES:
                    chunk = connection.recv(512)
                    if not chunk:
                        break
                    response.extend(chunk)
                    if b"\n" in chunk:
                        break
        except (OSError, TimeoutError) as error:
            raise ActivationError(f"activation endpoint unavailable: {host}:{port}") from error

        if len(response) > MAX_RESPONSE_BYTES:
            raise ActivationError("activation response exceeds size limit")
        raw = bytes(response).split(b"\n", 1)[0]
        try:
            decoded = raw.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as error:
            raise ActivationError("activation response is not valid UTF-8") from error
        if not decoded:
            raise ActivationError("activation endpoint returned an empty response")
        if decoded.startswith("ACTIVATE_REJECTED "):
            reason = decoded.removeprefix("ACTIVATE_REJECTED ")
            if not reason.startswith("reason=") or not SAFE_ID.fullmatch(reason[7:]):
                raise ActivationError("activation endpoint returned a malformed rejection")
            raise ActivationError(f"activation rejected: {reason[7:]}")
        if decoded.startswith("ACTIVATE_FAILURE "):
            raise ActivationError("activation endpoint reported an internal failure")
        return decoded

    def probe(self, host: str, port: int, token: str) -> str:
        """返回 ``warm`` 或 ``activated``；不接受其他状态。"""
        token = self._safe_id("claim token", token)
        response = self._request(host, port, f"PROBE_V1 {token}")
        match = re.fullmatch(r"PROBE_READY state=(warm|activated)", response)
        if match is None:
            raise ActivationError("unexpected PROBE_V1 response")
        return match.group(1)

    def activate(
        self,
        host: str,
        port: int,
        token: str,
        activation_id: str,
        volume_uuid: str,
    ) -> BoundReady:
        token = self._safe_id("claim token", token)
        activation_id = self._safe_id("activation ID", activation_id)
        volume_uuid = self._safe_uuid(volume_uuid)
        response = self._request(
            host,
            port,
            f"ACTIVATE_V1 {token} {activation_id} {volume_uuid}",
        )
        match = re.fullmatch(
            r"BOUND_READY activation_id=([A-Za-z0-9._:-]{1,128}) "
            r"rpc_address=([^: ]+):(\d{1,5})",
            response,
        )
        if match is None or match.group(1) != activation_id:
            raise ActivationError("unexpected ACTIVATE_V1 response")
        rpc_host = match.group(2)
        try:
            ipaddress.ip_address(rpc_host)
        except ValueError as error:
            raise ActivationError("BOUND_READY returned an invalid RPC address") from error
        rpc_port = int(match.group(3))
        if not 1 <= rpc_port <= 65535:
            raise ActivationError("BOUND_READY returned an invalid RPC port")
        return BoundReady(activation_id, rpc_host, rpc_port)

    def commit(
        self,
        host: str,
        port: int,
        token: str,
        activation_id: str,
    ) -> None:
        token = self._safe_id("claim token", token)
        activation_id = self._safe_id("activation ID", activation_id)
        response = self._request(host, port, f"COMMIT_V1 {token} {activation_id}")
        if response != f"COMMITTED activation_id={activation_id}":
            raise ActivationError("unexpected COMMIT_V1 response")
