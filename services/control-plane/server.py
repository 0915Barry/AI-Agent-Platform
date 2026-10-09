#!/usr/bin/env python3
"""M10-M14 单机控制面、Agent 任务、会话与工作区文件 HTTP API。

该服务把前端将来需要的“创建、启动、查询、心跳、停止、销毁”操作转换为
InstanceRuntime 调用。产品状态保存在 SQLite；Firecracker 的实时进程状态仍以
lifecycle.py 元数据和 /proc 为准，查询时会自动对两者进行校准。

M11 在同一控制面增加任务队列；M13 继续增加会话、消息和受限多轮上下文。guest
通过独立 TAP bridge 领取任务并回传事件。
当前没有身份认证，因此 main() 强制只允许 loopback 监听。macOS 开发者应通过
SSH 隧道访问，绝不能为了省事把监听地址改成 0.0.0.0。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_MANAGER_DIR = REPO_ROOT / "services" / "instance-manager"
sys.path.insert(0, str(INSTANCE_MANAGER_DIR))

from runtime import InstanceRuntime, RuntimeFailure  # noqa: E402
from task_store import TaskStore, TaskStoreError  # noqa: E402


INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
INSTANCE_ROUTE_RE = re.compile(
    r"^/api/instances/([a-z0-9][a-z0-9-]{0,62})(?:/(start|stop|heartbeat))?$"
)
TASK_COLLECTION_ROUTE_RE = re.compile(
    r"^/api/instances/([a-z0-9][a-z0-9-]{0,62})/tasks$"
)
TASK_ROUTE_RE = re.compile(
    r"^/api/instances/([a-z0-9][a-z0-9-]{0,62})/tasks/"
    r"(task-[0-9a-f]{16})(?:/(events|stream))?$"
)
CONVERSATION_COLLECTION_ROUTE_RE = re.compile(
    r"^/api/instances/([a-z0-9][a-z0-9-]{0,62})/conversations$"
)
CONVERSATION_ROUTE_RE = re.compile(
    r"^/api/instances/([a-z0-9][a-z0-9-]{0,62})/conversations/"
    r"(conversation-[0-9a-f]{16})(/messages)?$"
)
WORKSPACE_ROUTE_RE = re.compile(
    r"^/api/instances/([a-z0-9][a-z0-9-]{0,62})/workspace/(list|read|write|mkdir|delete)$"
)
MAX_WORKSPACE_FILE_BYTES = 5 * 1024 * 1024


class ApiError(Exception):
    """带 HTTP 状态码和稳定错误码的可预期 API 异常。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class InstanceStore:
    """SQLite 产品状态仓库。

    每次操作创建短连接，并显式提交/回滚/关闭，适配 ThreadingHTTPServer 的
    多线程请求模型；数据库不保存供应商密钥或 guest 文件内容。
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """提供具备事务和确定性关闭语义的数据库连接。"""
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        """启用 WAL 并幂等创建最小实例表。"""
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
        """把 SQLite snake_case 字段映射成 API camelCase 字段。"""
        return {
            "id": row["id"],
            "status": row["status"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "lastError": row["last_error"],
        }

    def create(self, instance_id: str) -> dict[str, Any]:
        """创建 created 状态记录；重复 ID 返回 409。"""
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
        """按 ID 查询记录；不存在时返回统一 404 语义。"""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id,status,created_at,updated_at,last_error FROM instances WHERE id=?",
                (instance_id,),
            ).fetchone()
        if row is None:
            raise ApiError(404, "instance_not_found", f"instance not found: {instance_id}")
        return self.row_to_dict(row)

    def list(self) -> list[dict[str, Any]]:
        """按创建时间稳定排序列出实例。"""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id,status,created_at,updated_at,last_error FROM instances ORDER BY created_at,id"
            ).fetchall()
        return [self.row_to_dict(row) for row in rows]

    def update(self, instance_id: str, status: str, last_error: str | None = None) -> dict[str, Any]:
        """更新状态和最后错误，供状态机每个阶段使用。"""
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE instances SET status=?,updated_at=?,last_error=? WHERE id=?",
                (status, int(time.time()), last_error, instance_id),
            )
        if cursor.rowcount != 1:
            raise ApiError(404, "instance_not_found", f"instance not found: {instance_id}")
        return self.get(instance_id)

    def delete(self, instance_id: str) -> None:
        """删除产品记录；实际磁盘必须先由 Runtime 销毁。"""
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM instances WHERE id=?", (instance_id,))
        if cursor.rowcount != 1:
            raise ApiError(404, "instance_not_found", f"instance not found: {instance_id}")


class ControlPlane:
    """协调产品状态机与高权限 InstanceRuntime。

    同一实例的变更使用进程内锁串行化，防止两个并发 start/stop 请求互相覆盖。
    不同实例仍可由 ThreadingHTTPServer 并行处理。
    """

    def __init__(self, store: InstanceStore, runtime: InstanceRuntime, tasks: TaskStore) -> None:
        self.store = store
        self.runtime = runtime
        self.tasks = tasks
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def lock_for(self, instance_id: str) -> threading.Lock:
        """惰性创建每实例锁，锁表本身由 guard 保护。"""
        with self._locks_guard:
            return self._locks.setdefault(instance_id, threading.Lock())

    @staticmethod
    def validate_requested_id(instance_id: str) -> str:
        """在进入文件系统和 Runtime 前拒绝非法实例 ID。"""
        if not INSTANCE_RE.fullmatch(instance_id):
            raise ApiError(
                400,
                "invalid_instance_id",
                "instance id must contain lowercase letters, digits, or hyphens and be at most 63 characters",
            )
        return instance_id

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        """建立逻辑实例；此时尚未分配磁盘或启动 microVM。"""
        requested = payload.get("id")
        if requested is None:
            instance_id = f"agent-{uuid.uuid4().hex[:12]}"
        elif not isinstance(requested, str):
            raise ApiError(400, "invalid_instance_id", "instance id must be a string")
        else:
            instance_id = self.validate_requested_id(requested)
        return self.store.create(instance_id)

    def reconcile(self, record: dict[str, Any]) -> dict[str, Any]:
        """用真实 VMM 状态修正可能过期的 SQLite 状态。

        例如独立 idle watcher 已停止实例，但控制面进程当时并未收到事件；下一次
        GET/LIST 会在这里把 running 自动修正为 stopped。
        """
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
        """返回单个实例及其经校准的运行时状态。"""
        return self.reconcile(self.store.get(self.validate_requested_id(instance_id)))

    def list(self) -> list[dict[str, Any]]:
        """列出全部实例，并逐个校准可能过期的状态。"""
        return [self.reconcile(record) for record in self.store.list()]

    def start(self, instance_id: str) -> dict[str, Any]:
        """执行 created/stopped/failed → starting → running 状态流转。"""
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
        """执行 running → stopping → stopped；停止不会删除数据盘。"""
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
            self.tasks.fail_running(instance_id, "instance stopped before task completed")
            return {**self.store.update(instance_id, "stopped"), "runtime": runtime_status}

    def heartbeat(self, instance_id: str) -> dict[str, Any]:
        """仅允许 running 实例刷新活动时间。"""
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
        """先销毁宿主资源，再删除 SQLite 记录，避免产生孤儿磁盘。"""
        instance_id = self.validate_requested_id(instance_id)
        with self.lock_for(instance_id):
            record = self.store.get(instance_id)
            try:
                self.runtime.destroy(instance_id)
            except (RuntimeFailure, subprocess.SubprocessError) as error:
                self.store.update(instance_id, "failed", str(error)[-4000:])
                raise ApiError(500, "instance_destroy_failed", str(error)) from error
            self.tasks.delete_instance(instance_id)
            self.store.delete(instance_id)
            return {"id": record["id"], "status": "destroyed"}

    def create_task(self, instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """为正在运行的实例排队一个用户任务，并刷新实例活动时间。"""

        instance_id = self.validate_requested_id(instance_id)
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ApiError(400, "invalid_prompt", "prompt must be a non-empty string")
        prompt = prompt.strip()
        if len(prompt.encode("utf-8")) > 32768:
            raise ApiError(413, "prompt_too_large", "prompt exceeds 32 KiB")
        record = self.get(instance_id)
        if record["status"] != "running":
            raise ApiError(409, "instance_not_running", f"instance is not running: {instance_id}")
        try:
            self.runtime.heartbeat(instance_id)
            return self.tasks.create(instance_id, prompt)
        except (RuntimeFailure, TaskStoreError, OSError, sqlite3.Error) as error:
            raise ApiError(500, "task_create_failed", str(error)) from error

    def get_task(self, instance_id: str, task_id: str) -> dict[str, Any]:
        """读取任务当前状态和最终输出。"""

        self.store.get(self.validate_requested_id(instance_id))
        try:
            return self.tasks.get(instance_id, task_id)
        except TaskStoreError as error:
            raise ApiError(404, "task_not_found", str(error)) from error

    def task_events(
        self, instance_id: str, task_id: str, after_sequence: int = 0
    ) -> list[dict[str, Any]]:
        """返回游标之后的任务事件，供 JSON 查询和 SSE 断线续传。"""

        self.store.get(self.validate_requested_id(instance_id))
        try:
            return self.tasks.events(instance_id, task_id, after_sequence)
        except TaskStoreError as error:
            raise ApiError(404, "task_not_found", str(error)) from error

    def create_conversation(self, instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """创建一个属于实例的持久化会话。"""

        instance_id = self.validate_requested_id(instance_id)
        self.store.get(instance_id)
        title = payload.get("title", "新会话")
        if not isinstance(title, str) or not title.strip():
            raise ApiError(400, "invalid_conversation_title", "conversation title must be a string")
        title = title.strip()
        if len(title.encode("utf-8")) > 160:
            raise ApiError(413, "conversation_title_too_large", "conversation title exceeds 160 bytes")
        try:
            return self.tasks.create_conversation(instance_id, title)
        except (TaskStoreError, OSError, sqlite3.Error) as error:
            raise ApiError(500, "conversation_create_failed", str(error)) from error

    def list_conversations(self, instance_id: str) -> list[dict[str, Any]]:
        instance_id = self.validate_requested_id(instance_id)
        self.store.get(instance_id)
        return self.tasks.list_conversations(instance_id)

    def get_conversation(self, instance_id: str, conversation_id: str) -> dict[str, Any]:
        instance_id = self.validate_requested_id(instance_id)
        self.store.get(instance_id)
        try:
            return self.tasks.get_conversation(instance_id, conversation_id)
        except TaskStoreError as error:
            raise ApiError(404, "conversation_not_found", str(error)) from error

    def conversation_messages(self, instance_id: str, conversation_id: str) -> list[dict[str, Any]]:
        instance_id = self.validate_requested_id(instance_id)
        self.store.get(instance_id)
        try:
            return self.tasks.list_messages(instance_id, conversation_id)
        except TaskStoreError as error:
            raise ApiError(404, "conversation_not_found", str(error)) from error

    def create_conversation_message(
        self, instance_id: str, conversation_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """保存用户消息，以受限历史构造任务，并刷新实例活动时间。"""

        instance_id = self.validate_requested_id(instance_id)
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ApiError(400, "invalid_message", "message content must be a non-empty string")
        content = content.strip()
        if len(content.encode("utf-8")) > 16384:
            raise ApiError(413, "message_too_large", "message content exceeds 16 KiB")
        record = self.get(instance_id)
        if record["status"] != "running":
            raise ApiError(409, "instance_not_running", f"instance is not running: {instance_id}")
        try:
            self.tasks.get_conversation(instance_id, conversation_id)
            self.runtime.heartbeat(instance_id)
            return self.tasks.create_conversation_task(instance_id, conversation_id, content)
        except TaskStoreError as error:
            if "conversation not found" in str(error):
                raise ApiError(404, "conversation_not_found", str(error)) from error
            raise ApiError(400, "conversation_context_invalid", str(error)) from error
        except (RuntimeFailure, OSError, sqlite3.Error) as error:
            raise ApiError(500, "conversation_task_create_failed", str(error)) from error

    @staticmethod
    def validate_workspace_path(value: Any, *, allow_root: bool) -> str:
        """把 API 路径限制为 /workspace 下的 POSIX 相对路径。"""

        if not isinstance(value, str):
            raise ApiError(400, "invalid_workspace_path", "workspace path must be a string")
        if value.startswith("/"):
            raise ApiError(400, "invalid_workspace_path", "workspace path must be relative")
        value = value.strip("/")
        if not value:
            if allow_root:
                return ""
            raise ApiError(400, "invalid_workspace_path", "workspace path must not be empty")
        if len(value.encode("utf-8")) > 1024 or "\\" in value or "\x00" in value:
            raise ApiError(400, "invalid_workspace_path", "workspace path is invalid or too long")
        parts = value.split("/")
        if any(part in {"", ".", ".."} or len(part.encode("utf-8")) > 255 for part in parts):
            raise ApiError(400, "invalid_workspace_path", "workspace path contains an invalid segment")
        return "/".join(parts)

    def workspace_operation(
        self, instance_id: str, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """排队文件操作并等待 guest 回传，避免宿主并发挂载运行中的 ext4。"""

        instance_id = self.validate_requested_id(instance_id)
        if action not in {"list", "read", "write", "mkdir", "delete"}:
            raise ApiError(404, "workspace_action_not_found", "workspace action not found")
        path = self.validate_workspace_path(payload.get("path", ""), allow_root=action == "list")
        request: dict[str, Any] = {}
        if action == "write":
            encoded = payload.get("contentBase64")
            if not isinstance(encoded, str):
                raise ApiError(400, "invalid_file_content", "contentBase64 must be a string")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as error:
                raise ApiError(400, "invalid_file_content", "contentBase64 is invalid") from error
            if len(decoded) > MAX_WORKSPACE_FILE_BYTES:
                raise ApiError(413, "workspace_file_too_large", "workspace file exceeds 5 MiB")
            request["contentBase64"] = encoded

        record = self.get(instance_id)
        if record["status"] != "running":
            raise ApiError(409, "instance_not_running", f"instance is not running: {instance_id}")
        try:
            self.runtime.heartbeat(instance_id)
            operation = self.tasks.create_workspace_operation(instance_id, action, path, request)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                operation = self.tasks.get_workspace_operation(instance_id, operation["id"])
                if operation["status"] == "completed":
                    return operation["result"]
                if operation["status"] == "failed":
                    raise ApiError(409, "workspace_operation_failed", operation["error"] or "operation failed")
                time.sleep(0.1)
            raise ApiError(504, "workspace_operation_timeout", "microVM did not finish the file operation")
        except ApiError:
            raise
        except (RuntimeFailure, TaskStoreError, OSError, sqlite3.Error) as error:
            raise ApiError(500, "workspace_operation_failed", str(error)) from error


class ControlPlaneHandler(BaseHTTPRequestHandler):
    """只接受固定路由和 JSON 对象的小型 HTTP 适配层。"""

    server_version = "AgentControlPlane/0.1"
    protocol_version = "HTTP/1.1"

    @property
    def control(self) -> ControlPlane:
        return self.server.control  # type: ignore[attr-defined]

    def log_message(self, message_format: str, *args: object) -> None:
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), message_format % args)
        )

    def send_payload(self, status: int, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        """发送紧凑 JSON，并禁止缓存动态实例状态。"""
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_payload(self, error: ApiError) -> None:
        """保持所有可预期错误具有相同 JSON 结构。"""
        self.send_payload(
            error.status,
            {"error": {"code": error.code, "message": error.message}},
        )

    def read_json(self, max_bytes: int = 65536) -> dict[str, Any]:
        """读取有明确上限的 JSON 对象，避免无限请求体占用 root 服务内存。"""
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise ApiError(400, "invalid_content_length", "invalid Content-Length") from error
        if length < 0 or length > max_bytes:
            raise ApiError(413, "request_too_large", "request body exceeds the allowed size")
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as error:
            raise ApiError(400, "invalid_json", "request body must be valid JSON") from error
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")
        return payload

    def stream_task_events(self, instance_id: str, task_id: str, after_sequence: int) -> None:
        """以 SSE 推送有序任务事件，并支持浏览器 Last-Event-ID 自动续传。

        控制面仍只绑定 loopback。这里不发送提示词、模型密钥或 Gateway 审计内容；
        只转发 TaskStore 已接受的脱敏运行事件。终态事件发出后主动结束连接。
        """

        task = self.control.get_task(instance_id, task_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        cursor = max(0, after_sequence)
        last_heartbeat = time.monotonic()
        try:
            while True:
                events = self.control.task_events(instance_id, task_id, cursor)
                for event in events:
                    cursor = event["sequence"]
                    body = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    frame = f"id: {cursor}\nevent: task-event\ndata: {body}\n\n"
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                    if event["type"] in {"completed", "failed"}:
                        self.close_connection = True
                        return

                # 终态可能来自旧库或异常恢复路径；没有新事件时也不能永久挂住。
                task = self.control.get_task(instance_id, task_id)
                if task["status"] in {"completed", "failed"}:
                    self.close_connection = True
                    return
                if time.monotonic() - last_heartbeat >= 10:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.monotonic()
                time.sleep(0.15)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_GET(self) -> None:
        """处理健康检查、实例列表和单实例查询。"""
        try:
            parsed = urlsplit(self.path)
            request_path = parsed.path
            if request_path == "/healthz":
                self.send_payload(200, {"status": "ok"})
                return
            if request_path == "/api/instances":
                self.send_payload(200, {"instances": self.control.list()})
                return
            conversation_collection = CONVERSATION_COLLECTION_ROUTE_RE.fullmatch(request_path)
            if conversation_collection:
                self.send_payload(
                    200,
                    {"conversations": self.control.list_conversations(conversation_collection.group(1))},
                )
                return
            conversation_match = CONVERSATION_ROUTE_RE.fullmatch(request_path)
            if conversation_match:
                instance_id, conversation_id, messages_suffix = conversation_match.groups()
                if messages_suffix:
                    self.send_payload(
                        200,
                        {"messages": self.control.conversation_messages(instance_id, conversation_id)},
                    )
                else:
                    self.send_payload(200, self.control.get_conversation(instance_id, conversation_id))
                return
            task_match = TASK_ROUTE_RE.fullmatch(request_path)
            if task_match:
                instance_id, task_id, task_suffix = task_match.groups()
                if task_suffix == "events":
                    after = parse_qs(parsed.query).get("after", ["0"])[0]
                    try:
                        after_sequence = int(after)
                    except ValueError as error:
                        raise ApiError(400, "invalid_event_cursor", "after must be an integer") from error
                    self.send_payload(
                        200,
                        {"events": self.control.task_events(instance_id, task_id, after_sequence)},
                    )
                elif task_suffix == "stream":
                    cursor_value = self.headers.get("Last-Event-ID")
                    if cursor_value is None:
                        cursor_value = parse_qs(parsed.query).get("after", ["0"])[0]
                    try:
                        cursor = int(cursor_value)
                    except ValueError as error:
                        raise ApiError(
                            400, "invalid_event_cursor", "Last-Event-ID must be an integer"
                        ) from error
                    self.stream_task_events(instance_id, task_id, cursor)
                else:
                    self.send_payload(200, self.control.get_task(instance_id, task_id))
                return
            match = INSTANCE_ROUTE_RE.fullmatch(request_path)
            if match and match.group(2) is None:
                self.send_payload(200, self.control.get(match.group(1)))
                return
            raise ApiError(404, "route_not_found", "route not found")
        except ApiError as error:
            self.send_error_payload(error)

    def do_POST(self) -> None:
        """处理实例创建以及 start/stop/heartbeat 动作。"""
        try:
            if self.path == "/api/instances":
                self.send_payload(201, self.control.create(self.read_json()))
                return
            workspace_match = WORKSPACE_ROUTE_RE.fullmatch(self.path)
            if workspace_match:
                instance_id, action = workspace_match.groups()
                limit = 8 * 1024 * 1024 if action == "write" else 65536
                self.send_payload(
                    200,
                    self.control.workspace_operation(instance_id, action, self.read_json(limit)),
                )
                return
            conversation_collection = CONVERSATION_COLLECTION_ROUTE_RE.fullmatch(self.path)
            if conversation_collection:
                self.send_payload(
                    201,
                    self.control.create_conversation(
                        conversation_collection.group(1), self.read_json()
                    ),
                )
                return
            conversation_match = CONVERSATION_ROUTE_RE.fullmatch(self.path)
            if conversation_match and conversation_match.group(3):
                instance_id, conversation_id, _ = conversation_match.groups()
                self.send_payload(
                    202,
                    self.control.create_conversation_message(
                        instance_id, conversation_id, self.read_json()
                    ),
                )
                return
            task_match = TASK_COLLECTION_ROUTE_RE.fullmatch(self.path)
            if task_match:
                self.send_payload(202, self.control.create_task(task_match.group(1), self.read_json()))
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
        """处理显式实例销毁；这是删除持久化数据的唯一 HTTP 操作。"""
        try:
            match = INSTANCE_ROUTE_RE.fullmatch(self.path)
            if not match or match.group(2) is not None:
                raise ApiError(404, "route_not_found", "route not found")
            self.send_payload(200, self.control.destroy(match.group(1)))
        except ApiError as error:
            self.send_error_payload(error)


def build_parser() -> argparse.ArgumentParser:
    """定义监听地址、数据库、生命周期状态目录和空闲阈值。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", default=18090, type=int)
    parser.add_argument("--database", type=Path, default=Path("/var/lib/fc/control-plane.db"))
    parser.add_argument("--state-root", type=Path, default=Path("/var/lib/fc"))
    parser.add_argument("--task-database", type=Path, default=Path("/var/lib/fc/tasks/tasks.db"))
    parser.add_argument("--deepseek-credential", type=Path)
    parser.add_argument("--idle-timeout", type=int, default=300)
    return parser


def main() -> None:
    """验证安全启动条件并运行多线程 loopback HTTP 服务。"""
    args = build_parser().parse_args()
    if os.geteuid() != 0:
        raise SystemExit("control plane must run as root")
    if args.listen_host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("unauthenticated control plane only supports loopback listeners")
    if args.idle_timeout < 1:
        raise SystemExit("idle timeout must be at least one second")

    store = InstanceStore(args.database)
    tasks = TaskStore(args.task_database)
    runtime = InstanceRuntime(
        state_root=args.state_root,
        idle_timeout_seconds=args.idle_timeout,
        deepseek_credential=args.deepseek_credential,
        task_database=args.task_database,
    )
    control = ControlPlane(store, runtime, tasks)
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
