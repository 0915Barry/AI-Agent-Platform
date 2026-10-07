#!/usr/bin/env python3
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_MANAGER_DIR = REPO_ROOT / "services" / "instance-manager"
sys.path.insert(0, str(INSTANCE_MANAGER_DIR))

from runtime import InstanceRuntime, RuntimeFailure  # noqa: E402


INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
INSTANCE_ROUTE_RE = re.compile(
    r"^/api/instances/([a-z0-9][a-z0-9-]{0,62})(?:/(start|stop|heartbeat))?$"
)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class InstanceStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS instances (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_error TEXT
                )
                """
            )

    @staticmethod
    def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "status": row["status"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "lastError": row["last_error"],
        }

    def create(self, instance_id: str) -> dict[str, Any]:
        now = int(time.time())
        try:
            with self.connect() as connection:
                connection.execute(
                    "INSERT INTO instances(id,status,created_at,updated_at,last_error) VALUES(?,?,?,?,NULL)",
                    (instance_id, "created", now, now),
                )
        except sqlite3.IntegrityError as error:
            raise ApiError(409, "instance_exists", f"instance already exists: {instance_id}") from error
        return self.get(instance_id)

    def get(self, instance_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id,status,created_at,updated_at,last_error FROM instances WHERE id=?",
                (instance_id,),
            ).fetchone()
        if row is None:
            raise ApiError(404, "instance_not_found", f"instance not found: {instance_id}")
        return self.row_to_dict(row)

    def list(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id,status,created_at,updated_at,last_error FROM instances ORDER BY created_at,id"
            ).fetchall()
        return [self.row_to_dict(row) for row in rows]

    def update(self, instance_id: str, status: str, last_error: str | None = None) -> dict[str, Any]:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE instances SET status=?,updated_at=?,last_error=? WHERE id=?",
                (status, int(time.time()), last_error, instance_id),
            )
        if cursor.rowcount != 1:
            raise ApiError(404, "instance_not_found", f"instance not found: {instance_id}")
        return self.get(instance_id)

    def delete(self, instance_id: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM instances WHERE id=?", (instance_id,))
        if cursor.rowcount != 1:
            raise ApiError(404, "instance_not_found", f"instance not found: {instance_id}")


class ControlPlane:
    def __init__(self, store: InstanceStore, runtime: InstanceRuntime) -> None:
        self.store = store
        self.runtime = runtime
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def lock_for(self, instance_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(instance_id, threading.Lock())

    @staticmethod
    def validate_requested_id(instance_id: str) -> str:
        if not INSTANCE_RE.fullmatch(instance_id):
            raise ApiError(
                400,
                "invalid_instance_id",
                "instance id must contain lowercase letters, digits, or hyphens and be at most 63 characters",
            )
        return instance_id

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested = payload.get("id")
        if requested is None:
            instance_id = f"agent-{uuid.uuid4().hex[:12]}"
        elif not isinstance(requested, str):
            raise ApiError(400, "invalid_instance_id", "instance id must be a string")
        else:
            instance_id = self.validate_requested_id(requested)
        return self.store.create(instance_id)

    def reconcile(self, record: dict[str, Any]) -> dict[str, Any]:
        if record["status"] not in {"running", "starting", "stopping"}:
            return record
        runtime_status = self.runtime.status(record["id"])
        if runtime_status is None:
            if record["status"] == "starting":
                return record
            return self.store.update(record["id"], "stopped")
        if runtime_status.get("status") == "stopped" or not runtime_status.get("processAlive", False):
            return self.store.update(record["id"], "stopped")
        return {**record, "runtime": runtime_status}

    def get(self, instance_id: str) -> dict[str, Any]:
        return self.reconcile(self.store.get(self.validate_requested_id(instance_id)))

    def list(self) -> list[dict[str, Any]]:
        return [self.reconcile(record) for record in self.store.list()]

    def start(self, instance_id: str) -> dict[str, Any]:
        instance_id = self.validate_requested_id(instance_id)
        with self.lock_for(instance_id):
            record = self.get(instance_id)
            if record["status"] == "running":
                raise ApiError(409, "instance_running", f"instance is already running: {instance_id}")
            if record["status"] in {"starting", "stopping"}:
                raise ApiError(409, "instance_busy", f"instance is currently {record['status']}: {instance_id}")
            self.store.update(instance_id, "starting")
            try:
                runtime_status = self.runtime.start(instance_id)
            except (RuntimeFailure, OSError, subprocess.SubprocessError) as error:
                self.store.update(instance_id, "failed", str(error)[-4000:])
                raise ApiError(500, "instance_start_failed", str(error)) from error
            return {**self.store.update(instance_id, "running"), "runtime": runtime_status}

    def stop(self, instance_id: str) -> dict[str, Any]:
        instance_id = self.validate_requested_id(instance_id)
        with self.lock_for(instance_id):
            record = self.get(instance_id)
            if record["status"] in {"created", "stopped", "failed"}:
                raise ApiError(409, "instance_not_running", f"instance is not running: {instance_id}")
            if record["status"] in {"starting", "stopping"}:
                raise ApiError(409, "instance_busy", f"instance is currently {record['status']}: {instance_id}")
            self.store.update(instance_id, "stopping")
            try:
                runtime_status = self.runtime.stop(instance_id)
            except (RuntimeFailure, subprocess.SubprocessError) as error:
                self.store.update(instance_id, "failed", str(error)[-4000:])
                raise ApiError(500, "instance_stop_failed", str(error)) from error
            return {**self.store.update(instance_id, "stopped"), "runtime": runtime_status}

    def heartbeat(self, instance_id: str) -> dict[str, Any]:
        instance_id = self.validate_requested_id(instance_id)
        record = self.get(instance_id)
        if record["status"] != "running":
            raise ApiError(409, "instance_not_running", f"instance is not running: {instance_id}")
        try:
            runtime_status = self.runtime.heartbeat(instance_id)
        except (RuntimeFailure, subprocess.SubprocessError) as error:
            raise ApiError(500, "heartbeat_failed", str(error)) from error
        return {**record, "runtime": runtime_status}

    def destroy(self, instance_id: str) -> dict[str, Any]:
        instance_id = self.validate_requested_id(instance_id)
        with self.lock_for(instance_id):
            record = self.store.get(instance_id)
            try:
                self.runtime.destroy(instance_id)
            except (RuntimeFailure, subprocess.SubprocessError) as error:
                self.store.update(instance_id, "failed", str(error)[-4000:])
                raise ApiError(500, "instance_destroy_failed", str(error)) from error
            self.store.delete(instance_id)
            return {"id": record["id"], "status": "destroyed"}


class ControlPlaneHandler(BaseHTTPRequestHandler):
    server_version = "AgentControlPlane/0.1"

    @property
    def control(self) -> ControlPlane:
        return self.server.control  # type: ignore[attr-defined]

    def log_message(self, message_format: str, *args: object) -> None:
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), message_format % args)
        )

    def send_payload(self, status: int, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_payload(self, error: ApiError) -> None:
        self.send_payload(
            error.status,
            {"error": {"code": error.code, "message": error.message}},
        )

    def read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise ApiError(400, "invalid_content_length", "invalid Content-Length") from error
        if length < 0 or length > 65536:
            raise ApiError(413, "request_too_large", "request body exceeds 64 KiB")
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as error:
            raise ApiError(400, "invalid_json", "request body must be valid JSON") from error
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")
        return payload

    def do_GET(self) -> None:
        try:
            if self.path == "/healthz":
                self.send_payload(200, {"status": "ok"})
                return
            if self.path == "/api/instances":
                self.send_payload(200, {"instances": self.control.list()})
                return
            match = INSTANCE_ROUTE_RE.fullmatch(self.path)
            if match and match.group(2) is None:
                self.send_payload(200, self.control.get(match.group(1)))
                return
            raise ApiError(404, "route_not_found", "route not found")
        except ApiError as error:
            self.send_error_payload(error)

    def do_POST(self) -> None:
        try:
            if self.path == "/api/instances":
                self.send_payload(201, self.control.create(self.read_json()))
                return
            match = INSTANCE_ROUTE_RE.fullmatch(self.path)
            if not match or match.group(2) is None:
                raise ApiError(404, "route_not_found", "route not found")
            instance_id, action = match.groups()
            if action == "start":
                self.send_payload(200, self.control.start(instance_id))
            elif action == "stop":
                self.send_payload(200, self.control.stop(instance_id))
            elif action == "heartbeat":
                self.send_payload(200, self.control.heartbeat(instance_id))
            else:
                raise ApiError(404, "route_not_found", "route not found")
        except ApiError as error:
            self.send_error_payload(error)

    def do_DELETE(self) -> None:
        try:
            match = INSTANCE_ROUTE_RE.fullmatch(self.path)
            if not match or match.group(2) is not None:
                raise ApiError(404, "route_not_found", "route not found")
            self.send_payload(200, self.control.destroy(match.group(1)))
        except ApiError as error:
            self.send_error_payload(error)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", default=18090, type=int)
    parser.add_argument("--database", type=Path, default=Path("/var/lib/fc/control-plane.db"))
    parser.add_argument("--state-root", type=Path, default=Path("/var/lib/fc"))
    parser.add_argument("--idle-timeout", type=int, default=300)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if os.geteuid() != 0:
        raise SystemExit("control plane must run as root")
    if args.listen_host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("M10 control plane only supports loopback listeners")
    if args.idle_timeout < 1:
        raise SystemExit("idle timeout must be at least one second")

    store = InstanceStore(args.database)
    runtime = InstanceRuntime(
        state_root=args.state_root,
        idle_timeout_seconds=args.idle_timeout,
    )
    control = ControlPlane(store, runtime)
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), ControlPlaneHandler)
    server.control = control  # type: ignore[attr-defined]
    print(
        f"CONTROL_PLANE_READY bind={args.listen_host}:{args.listen_port} "
        f"idle_timeout={args.idle_timeout}s auth=loopback-only",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
