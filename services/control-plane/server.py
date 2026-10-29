#!/usr/bin/env python3
"""实例控制面、认证、Agent 任务与会话 HTTP API。

该服务把前端将来需要的“创建、启动、查询、心跳、停止、销毁”操作转换为
InstanceRuntime 调用。产品状态保存在 SQLite；Firecracker 的实时进程状态仍以
lifecycle.py 元数据和 /proc 为准，查询时会自动对两者进行校准。

任务队列保存一次执行的状态与事件；会话和消息提供受限的多轮上下文。宿主机
Pi Gateway 通过实例独立 TAP 主动连接 Pi RPC，并把原生事件写回任务存储。
服务可启用本地账户、HttpOnly 会话和实例所有权。即便启用认证，服务仍强制只允许
loopback 监听；开发者应通过 SSH 隧道访问。
"""

from __future__ import annotations

import argparse
import hashlib
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
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_MANAGER_DIR = REPO_ROOT / "services" / "instance-manager"
PI_GATEWAY_DIR = REPO_ROOT / "services" / "pi-gateway"
sys.path.insert(0, str(INSTANCE_MANAGER_DIR))
sys.path.insert(0, str(PI_GATEWAY_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime import InstanceRuntime, RuntimeFailure  # noqa: E402
from auth import AuthStore  # noqa: E402
from gateway import PiGateway, PiGatewayError  # noqa: E402
from task_store import TaskStore, TaskStoreError  # noqa: E402
from workspace_store import WorkspaceStore, WorkspaceStoreError  # noqa: E402


INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# 路由层只接受受控格式的实例、任务、工作区和会话 ID。这样可以在这些值进入
# SQLite 查询或文件系统路径之前拦截路径穿越和意外字符。格式校验并不等于权限
# 校验：资源归属仍由 authorize_instance() 和 WorkspaceStore 分别检查。
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
WORKSPACE_COLLECTION_ROUTE = "/api/workspaces"
WORKSPACE_ROUTE_RE = re.compile(
    r"^/api/workspaces/(workspace-[0-9a-f]{16})(?:/(select|conversations))?$"
)
WORKSPACE_CONVERSATION_ROUTE_RE = re.compile(
    r"^/api/workspaces/(workspace-[0-9a-f]{16})/conversations/"
    r"(conversation-[0-9a-f]{16})$"
)
MAX_INSTANCES_PER_USER = 1
MAX_RUNNING_INSTANCES_PER_USER = 1
SESSION_COOKIE_NAME = "aap_session"


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
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(instances)").fetchall()
            }
            if "owner_id" not in columns:
                connection.execute("ALTER TABLE instances ADD COLUMN owner_id TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS instances_owner_created "
                "ON instances(owner_id,created_at,id)"
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
            "ownerId": row["owner_id"],
        }

    def create(self, instance_id: str, owner_id: str | None = None) -> dict[str, Any]:
        """创建 created 状态记录；重复 ID 返回 409。"""
        now = int(time.time())
        try:
            with self.connect() as connection:
                connection.execute(
                    "INSERT INTO instances(id,status,created_at,updated_at,last_error,owner_id) "
                    "VALUES(?,?,?,?,NULL,?)",
                    (instance_id, "created", now, now, owner_id),
                )
        except sqlite3.IntegrityError as error:
            raise ApiError(409, "instance_exists", f"instance already exists: {instance_id}") from error
        return self.get(instance_id)

    def get(self, instance_id: str) -> dict[str, Any]:
        """按 ID 查询记录；不存在时返回统一 404 语义。"""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM instances WHERE id=?",
                (instance_id,),
            ).fetchone()
        if row is None:
            raise ApiError(404, "instance_not_found", f"instance not found: {instance_id}")
        return self.row_to_dict(row)

    def list(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        """按创建时间稳定排序列出实例。"""
        with self.connect() as connection:
            if owner_id is None:
                rows = connection.execute(
                    "SELECT * FROM instances ORDER BY created_at,id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM instances WHERE owner_id=? ORDER BY created_at,id",
                    (owner_id,),
                ).fetchall()
        return [self.row_to_dict(row) for row in rows]

    def primary_for_owner(self, owner_id: str) -> dict[str, Any] | None:
        """返回用户后台运行槽位；优先选择当前活动实例。"""

        records = self.list(owner_id)
        if not records:
            return None
        return next(
            (record for record in records if record["status"] in {"starting", "running"}),
            records[0],
        )

    def assert_owner(self, instance_id: str, owner_id: str) -> None:
        """用 404 隐藏其他用户的实例是否存在，防止枚举资源 ID。"""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM instances WHERE id=? AND owner_id=?", (instance_id, owner_id)
            ).fetchone()
        if row is None:
            raise ApiError(404, "instance_not_found", f"instance not found: {instance_id}")

    def count_for_owner(self, owner_id: str, statuses: tuple[str, ...] | None = None) -> int:
        with self.connect() as connection:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                row = connection.execute(
                    f"SELECT COUNT(*) AS count FROM instances WHERE owner_id=? "
                    f"AND status IN ({placeholders})",
                    (owner_id, *statuses),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM instances WHERE owner_id=?", (owner_id,)
                ).fetchone()
        return int(row["count"])

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
    """连接业务数据、实例运行时和 Pi RPC 的核心编排模块。

    InstanceStore 保存实例的产品状态；InstanceRuntime 实际创建 TAP、Jailer 和
    Firecracker；WorkspaceStore 与 TaskStore 保存工作区、会话、消息和任务；
    PiGateway 则把已排队任务发送到 Guest。HTTP Handler 只负责协议适配，不直接
    操作这些底层组件。

    同一实例的变更使用进程内锁串行化，防止两个并发 start/stop 请求互相覆盖。
    不同实例仍可由 ThreadingHTTPServer 并行处理。
    """

    def __init__(
        self,
        store: InstanceStore,
        runtime: InstanceRuntime,
        tasks: TaskStore,
        pi_gateway: PiGateway | None = None,
        workspaces: WorkspaceStore | None = None,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.tasks = tasks
        self.pi_gateway = pi_gateway
        self.workspaces = workspaces
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}
        # 配额检查和状态写入必须形成一个临界区，否则两个不同实例的并发请求可能
        # 同时通过检查，使总实例数或运行实例数短暂突破上限。
        self._quota_guard = threading.Lock()
        # 每个实例最多只有一个后台分发线程，从而保持 Pi RPC 与工作区修改串行。
        self._dispatch_guard = threading.Lock()
        self._dispatching: set[str] = set()

    def lock_for(self, instance_id: str) -> threading.RLock:
        """惰性创建每实例锁，锁表本身由 guard 保护。"""
        with self._locks_guard:
            return self._locks.setdefault(instance_id, threading.RLock())

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

    def create(self, payload: dict[str, Any], owner_id: str | None = None) -> dict[str, Any]:
        """建立逻辑实例；此时尚未分配磁盘或启动 microVM。"""
        requested = payload.get("id")
        if requested is None:
            instance_id = f"agent-{uuid.uuid4().hex[:12]}"
        elif not isinstance(requested, str):
            raise ApiError(400, "invalid_instance_id", "instance id must be a string")
        else:
            instance_id = self.validate_requested_id(requested)
        if owner_id is None:
            return self.store.create(instance_id, owner_id)
        with self._quota_guard:
            if self.store.count_for_owner(owner_id) >= MAX_INSTANCES_PER_USER:
                raise ApiError(
                    409,
                    "instance_quota_exceeded",
                    f"每个用户最多创建 {MAX_INSTANCES_PER_USER} 个实例",
                )
            return self.store.create(instance_id, owner_id)

    def ensure_user_instance(self, owner_id: str) -> dict[str, Any]:
        """为工作区会话取得用户后台实例，用户无需感知实例 ID。"""

        # 工作区和会话可并发创建，因此“查询—创建”必须与实例配额
        # 共用同一把锁，避免同一用户在空记录上被同时创建两次。
        with self._quota_guard:
            existing = self.store.primary_for_owner(owner_id)
            if existing is not None:
                return existing
            digest = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:12]
            return self.store.create(f"agent-{digest}", owner_id)

    def require_workspace_store(self) -> WorkspaceStore:
        if self.workspaces is None:
            raise ApiError(503, "workspaces_unavailable", "工作区功能尚未启用")
        return self.workspaces

    def list_workspaces(self, owner_id: str) -> list[dict[str, Any]]:
        store = self.require_workspace_store()
        default = store.ensure_default(owner_id)
        for instance in self.store.list(owner_id):
            self.tasks.assign_unscoped_conversations(instance["id"], default["id"])
        return store.list(owner_id)

    def create_workspace(self, owner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        name = payload.get("name")
        if not isinstance(name, str):
            raise ApiError(400, "invalid_workspace_name", "工作区名称不能为空")
        try:
            return self.require_workspace_store().create(owner_id, name)
        except WorkspaceStoreError as error:
            raise ApiError(409, "workspace_invalid", str(error)) from error

    def select_workspace(self, owner_id: str, workspace_id: str) -> dict[str, Any]:
        try:
            return self.require_workspace_store().select(owner_id, workspace_id)
        except WorkspaceStoreError as error:
            raise ApiError(404, "workspace_not_found", str(error)) from error

    def list_workspace_conversations(
        self, owner_id: str, workspace_id: str
    ) -> list[dict[str, Any]]:
        try:
            self.require_workspace_store().get(owner_id, workspace_id)
        except WorkspaceStoreError as error:
            raise ApiError(404, "workspace_not_found", str(error)) from error
        return self.tasks.list_workspace_conversations(workspace_id)

    def create_workspace_conversation(
        self, owner_id: str, workspace_id: str
    ) -> dict[str, Any]:
        try:
            self.require_workspace_store().get(owner_id, workspace_id)
        except WorkspaceStoreError as error:
            raise ApiError(404, "workspace_not_found", str(error)) from error
        # 会话必须关联一个稳定的实例记录，后续任务才能找到用户数据盘与运行端点。
        # ensure_user_instance() 这里只补齐数据库关系，不会立即启动 microVM；
        # 用户发送第一条消息时才按需启动计算资源。
        instance = self.ensure_user_instance(owner_id)
        return self.tasks.create_conversation(
            instance["id"], "新会话", workspace_id=workspace_id
        )

    def delete_workspace_conversation(
        self, owner_id: str, workspace_id: str, conversation_id: str
    ) -> dict[str, Any]:
        """删除会话数据，不触及该工作区中其他文件。"""

        try:
            self.require_workspace_store().get(owner_id, workspace_id)
            deleted = self.tasks.delete_conversation(workspace_id, conversation_id)
        except WorkspaceStoreError as error:
            raise ApiError(404, "workspace_not_found", str(error)) from error
        except TaskStoreError as error:
            status = 409 if "正在" in str(error) else 404
            code = "conversation_busy" if status == 409 else "conversation_not_found"
            raise ApiError(status, code, str(error)) from error
        return {"id": deleted["id"], "status": "deleted"}

    def delete_workspace(self, owner_id: str, workspace_id: str) -> dict[str, Any]:
        """停止空闲计算资源，再删除工作区文件与数据库关系。"""

        store = self.require_workspace_store()
        try:
            workspace = store.get(owner_id, workspace_id)
        except WorkspaceStoreError as error:
            raise ApiError(404, "workspace_not_found", str(error)) from error
        instance = self.store.primary_for_owner(owner_id)
        if instance is None:
            return self._delete_workspace_resources(owner_id, workspace)

        # 删目录时数据盘不能同时被新 microVM 挂载。与 start/stop
        # 共用实例锁，将“检查—停机—离线挂载—删除”串行化。
        with self.lock_for(instance["id"]):
            instance = self.reconcile(self.store.get(instance["id"]))
            if instance["status"] in {"starting", "stopping"}:
                raise ApiError(409, "instance_busy", f"instance is {instance['status']}")
            if self.tasks.workspace_has_active_tasks(workspace_id):
                raise ApiError(409, "workspace_busy", "工作区仍有正在排队或执行的任务")
            if self.tasks.instance_has_active_tasks(instance["id"]):
                raise ApiError(409, "instance_busy", "用户实例正在执行其他工作区的任务")
            if instance["status"] == "running":
                self.stop(instance["id"])
            return self._delete_workspace_resources(owner_id, workspace)

    def _delete_workspace_resources(
        self, owner_id: str, workspace: dict[str, Any]
    ) -> dict[str, Any]:
        """调用方持有实例锁后，执行磁盘和数据库的实际清理。"""

        workspace_id = workspace["id"]
        store = self.require_workspace_store()
        try:
            if hasattr(self.runtime, "delete_workspace_data"):
                self.runtime.delete_workspace_data(owner_id, workspace["directoryName"])
            conversation_count = self.tasks.delete_workspace_conversations(workspace_id)
            store.delete(owner_id, workspace_id)
            current = store.ensure_default(owner_id)
        except (RuntimeFailure, OSError, subprocess.SubprocessError) as error:
            raise ApiError(500, "workspace_cleanup_failed", str(error)) from error
        except TaskStoreError as error:
            raise ApiError(409, "workspace_busy", str(error)) from error
        return {
            "id": workspace_id,
            "status": "deleted",
            "deletedConversations": conversation_count,
            "currentWorkspace": current,
        }

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

    def list(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        """列出全部实例，并逐个校准可能过期的状态。"""
        return [self.reconcile(record) for record in self.store.list(owner_id)]

    def start(self, instance_id: str, owner_id: str | None = None) -> dict[str, Any]:
        """执行 created/stopped/failed → starting → running 状态流转。"""
        instance_id = self.validate_requested_id(instance_id)
        with self.lock_for(instance_id):
            record = self.get(instance_id)
            if record["status"] == "running":
                raise ApiError(409, "instance_running", f"instance is already running: {instance_id}")
            if record["status"] in {"starting", "stopping"}:
                raise ApiError(409, "instance_busy", f"instance is currently {record['status']}: {instance_id}")
            if owner_id is None:
                self.store.update(instance_id, "starting")
            else:
                with self._quota_guard:
                    # 先校准同一用户可能已被空闲回收器关闭的实例，再计算运行配额。
                    self.list(owner_id)
                    if self.store.count_for_owner(
                        owner_id, ("starting", "running")
                    ) >= MAX_RUNNING_INSTANCES_PER_USER:
                        raise ApiError(
                            409,
                            "running_quota_exceeded",
                            f"每个用户最多同时运行 {MAX_RUNNING_INSTANCES_PER_USER} 个实例",
                        )
                    # 在释放配额锁之前写入 starting；下一请求会把它计入配额。
                    self.store.update(instance_id, "starting")
            try:
                runtime_status = self.runtime.start(instance_id, owner_id)
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
            task = self.tasks.create(instance_id, prompt)
            self.schedule_tasks(instance_id)
            return task
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

    def conversation_messages_page(
        self,
        instance_id: str,
        conversation_id: str,
        limit: int,
        before_sequence: int | None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """为终端和页面返回有上限的历史消息，避免长会话被一次性载入。"""

        instance_id = self.validate_requested_id(instance_id)
        self.store.get(instance_id)
        try:
            return self.tasks.list_messages_page(
                instance_id, conversation_id, limit, before_sequence
            )
        except TaskStoreError as error:
            raise ApiError(404, "conversation_not_found", str(error)) from error

    def create_conversation_message(
        self,
        instance_id: str,
        conversation_id: str,
        payload: dict[str, Any],
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        """保存用户消息，以受限历史构造任务，并刷新实例活动时间。

        对终端和未来页面而言，“发送消息”是一个业务动作。若实例已经停止，这里
        会先按用户身份恢复实例，再创建任务；调用方不需要自己编排 start/chat。
        """

        instance_id = self.validate_requested_id(instance_id)
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ApiError(400, "invalid_message", "message content must be a non-empty string")
        content = content.strip()
        if len(content.encode("utf-8")) > 16384:
            raise ApiError(413, "message_too_large", "message content exceeds 16 KiB")
        record = self.get(instance_id)
        # 把实例生命周期隐藏在聊天接口后面：用户关注会话，Control Plane 负责确保
        # 承载该用户数据盘的 microVM 已经可用。
        if record["status"] != "running" and owner_id is not None:
            record = self.start(instance_id, owner_id)
        if record["status"] != "running":
            raise ApiError(409, "instance_not_running", f"instance is not running: {instance_id}")
        try:
            self.tasks.get_conversation(instance_id, conversation_id)
            self.runtime.heartbeat(instance_id)
            # TaskStore 在一个事务中写入用户消息、截取受限历史并创建 queued 任务，
            # 避免出现“消息已保存但没有任务”或相反的半完成状态。
            task = self.tasks.create_conversation_task(
                instance_id,
                conversation_id,
                content,
            )
            self.schedule_tasks(instance_id)
            return task
        except TaskStoreError as error:
            if "conversation not found" in str(error):
                raise ApiError(404, "conversation_not_found", str(error)) from error
            raise ApiError(400, "conversation_context_invalid", str(error)) from error
        except (RuntimeFailure, OSError, sqlite3.Error) as error:
            raise ApiError(500, "conversation_task_create_failed", str(error)) from error

    def schedule_tasks(self, instance_id: str) -> None:
        """确保该实例存在一个后台消费者，把 queued 任务发给 Pi RPC。

        消息接口在任务持久化后即可返回 202；模型调用由该线程继续执行。每个实例
        最多一个消费者，既保持会话事件顺序，也避免两个任务同时修改同一数据盘。
        """

        if self.pi_gateway is None:
            # 单元测试和旧 smoke test 可以显式领取任务；正式服务总会注入 Gateway。
            return
        with self._dispatch_guard:
            if instance_id in self._dispatching:
                return
            self._dispatching.add(instance_id)
            thread = threading.Thread(
                target=self._dispatch_tasks,
                args=(instance_id,),
                name=f"pi-gateway-{instance_id}",
                daemon=True,
            )
            thread.start()

    def _dispatch_tasks(self, instance_id: str) -> None:
        """按创建顺序原子领取并执行同一实例的任务，直到队列为空。"""

        try:
            while True:
                # claim_next() 使用 SQLite 事务把 queued 改为 running，多个请求即使
                # 同时触发调度，也只有一个消费者能够领取同一任务。
                task = self.tasks.claim_next(instance_id)
                if task is None:
                    # 在 guard 内再查一次，封住“刚判断为空、另一请求恰好创建任务”的
                    # 竞态窗口，避免新任务一直等到下一次请求才被执行。
                    with self._dispatch_guard:
                        task = self.tasks.claim_next(instance_id)
                        if task is None:
                            self._dispatching.discard(instance_id)
                            return
                self._run_pi_task(instance_id, task)
        except Exception as error:
            # 后台线程不能让控制面退出。未执行的任务仍保持 queued，下一次提交可重启分发。
            print(f"PI_GATEWAY_DISPATCH_ERROR instance={instance_id} error={error}", file=sys.stderr)
            with self._dispatch_guard:
                self._dispatching.discard(instance_id)

    def _run_pi_task(self, instance_id: str, task: dict[str, Any]) -> None:
        """解析 Guest 端点、执行一次 Pi RPC，并把事件写回 TaskStore。

        TaskStore 提供 prompt 与 workspaceId，InstanceRuntime 提供 Guest IP 和 RPC
        端口；PiGateway 产生的文本增量、工具事件和终态会被统一保存，供 SSE、CLI
        或未来页面读取。
        """

        assert self.pi_gateway is not None
        task_id = task["id"]
        last_heartbeat = 0.0

        def save_event(event_type: str, payload: dict[str, Any]) -> None:
            nonlocal last_heartbeat
            self.tasks.append_event(instance_id, task_id, event_type, payload)
            now = time.monotonic()
            # 流式 token 可能非常密集。心跳最多每十秒刷新一次，既保护正在生成的
            # 实例不被空闲回收，又避免每个 token 都触发运行时状态写入。
            if now - last_heartbeat >= 10:
                self.runtime.heartbeat(instance_id)
                last_heartbeat = now

        try:
            runtime_status = self.runtime.status(instance_id)
            if runtime_status is None or not runtime_status.get("processAlive", False):
                raise PiGatewayError("instance runtime is not alive")
            guest_ip = runtime_status.get("guestIp")
            rpc_port = runtime_status.get("rpcPort")
            if not isinstance(guest_ip, str) or not isinstance(rpc_port, int):
                raise PiGatewayError("instance runtime has no Pi RPC endpoint")
            if hasattr(self.runtime, "busy"):
                self.runtime.busy(instance_id, True)
            self.runtime.heartbeat(instance_id)
            output = self.pi_gateway.execute(
                guest_ip,
                rpc_port,
                task["prompt"],
                save_event,
                task.get("workspaceId"),
            )
            # PiGateway 只有收到 agent_settled 才会返回，因此这里的 completed 表示
            # 模型循环及其中的工具调用均已真正结束，而不只是收到最后一个文本块。
            self.tasks.append_event(instance_id, task_id, "completed", {"output": output})
            self.runtime.heartbeat(instance_id)
        except Exception as error:
            try:
                self.tasks.append_event(
                    instance_id,
                    task_id,
                    "failed",
                    {"error": str(error)[:4000]},
                )
            except TaskStoreError:
                # stop() 可能已经把运行中任务标为失败，不重复覆盖终态。
                pass
        finally:
            if hasattr(self.runtime, "busy"):
                try:
                    self.runtime.busy(instance_id, False)
                except (RuntimeFailure, OSError):
                    pass

class ControlPlaneHandler(BaseHTTPRequestHandler):
    """把 HTTP 请求适配为 ControlPlane 方法调用。

    该层只负责路由、认证、JSON 校验与响应格式，不直接写 SQL，也不操作
    Firecracker 或 Pi RPC。因此 CLI、Web 和 Desktop 可以复用同一组业务接口。
    """

    server_version = "AgentControlPlane/0.1"
    protocol_version = "HTTP/1.1"

    @property
    def control(self) -> ControlPlane:
        return self.server.control  # type: ignore[attr-defined]

    @property
    def auth_store(self) -> AuthStore | None:
        return getattr(self.server, "auth_store", None)

    def log_message(self, message_format: str, *args: object) -> None:
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), message_format % args)
        )

    def send_payload(
        self,
        status: int,
        payload: dict[str, Any] | list[dict[str, Any]],
        headers: dict[str, str] | None = None,
    ) -> None:
        """发送紧凑 JSON，并禁止缓存动态实例状态。"""
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
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

    def session_token(self) -> str | None:
        raw_cookie = self.headers.get("Cookie")
        if not raw_cookie:
            return None
        try:
            cookie = SimpleCookie(raw_cookie)
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE_NAME)
        return None if morsel is None else morsel.value

    def current_user(self) -> dict[str, Any] | None:
        """把 Cookie 中的随机会话令牌解析为平台用户。

        客户端只保存令牌，不保存用户密码；AuthStore 负责令牌有效期和 user_id
        映射。未启用认证的底层 smoke test 返回 None。
        """
        if self.auth_store is None:
            return None
        token = self.session_token()
        return None if token is None else self.auth_store.session_user(token)

    def require_user(self) -> dict[str, Any] | None:
        user = self.current_user()
        if self.auth_store is not None and user is None:
            raise ApiError(401, "authentication_required", "请先登录")
        return user

    def authorize_instance(self, instance_id: str, user: dict[str, Any] | None) -> None:
        """确认实例属于当前用户，防止仅凭猜中实例 ID 跨用户访问。"""
        if user is not None:
            self.control.store.assert_owner(instance_id, user["id"])

    def stream_task_events(self, instance_id: str, task_id: str, after_sequence: int) -> None:
        """以 SSE 推送有序任务事件，并支持客户端 Last-Event-ID 续传。

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
            if request_path == "/api/auth/session":
                user = self.current_user()
                self.send_payload(200, {"authenticated": user is not None, "user": user})
                return
            user = self.require_user()
            if request_path == WORKSPACE_COLLECTION_ROUTE:
                if user is None:
                    raise ApiError(404, "route_not_found", "route not found")
                self.send_payload(200, {"workspaces": self.control.list_workspaces(user["id"])})
                return
            workspace_match = WORKSPACE_ROUTE_RE.fullmatch(request_path)
            if workspace_match and workspace_match.group(2) == "conversations":
                if user is None:
                    raise ApiError(404, "route_not_found", "route not found")
                self.send_payload(
                    200,
                    {
                        "conversations": self.control.list_workspace_conversations(
                            user["id"], workspace_match.group(1)
                        )
                    },
                )
                return
            if request_path == "/api/instances":
                self.send_payload(
                    200,
                    {"instances": self.control.list(user["id"] if user is not None else None)},
                )
                return
            conversation_collection = CONVERSATION_COLLECTION_ROUTE_RE.fullmatch(request_path)
            if conversation_collection:
                self.authorize_instance(conversation_collection.group(1), user)
                self.send_payload(
                    200,
                    {"conversations": self.control.list_conversations(conversation_collection.group(1))},
                )
                return
            conversation_match = CONVERSATION_ROUTE_RE.fullmatch(request_path)
            if conversation_match:
                instance_id, conversation_id, messages_suffix = conversation_match.groups()
                self.authorize_instance(instance_id, user)
                if messages_suffix:
                    query = parse_qs(parsed.query)
                    try:
                        limit = int(query.get("limit", ["50"])[0])
                        before_value = query.get("before", [None])[0]
                        before = None if before_value is None else int(before_value)
                    except (TypeError, ValueError) as error:
                        raise ApiError(
                            400, "invalid_message_cursor", "limit and before must be integers"
                        ) from error
                    if limit < 1 or limit > 100 or (before is not None and before < 1):
                        raise ApiError(
                            400,
                            "invalid_message_cursor",
                            "limit must be 1-100 and before must be positive",
                        )
                    messages, next_before = self.control.conversation_messages_page(
                        instance_id, conversation_id, limit, before
                    )
                    self.send_payload(
                        200,
                        {"messages": messages, "nextBefore": next_before},
                    )
                else:
                    self.send_payload(200, self.control.get_conversation(instance_id, conversation_id))
                return
            task_match = TASK_ROUTE_RE.fullmatch(request_path)
            if task_match:
                instance_id, task_id, task_suffix = task_match.groups()
                self.authorize_instance(instance_id, user)
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
                self.authorize_instance(match.group(1), user)
                self.send_payload(200, self.control.get(match.group(1)))
                return
            raise ApiError(404, "route_not_found", "route not found")
        except ApiError as error:
            self.send_error_payload(error)

    def do_POST(self) -> None:
        """处理实例创建以及 start/stop/heartbeat 动作。"""
        try:
            request_path = urlsplit(self.path).path
            if request_path == "/api/auth/login":
                if self.auth_store is None:
                    raise ApiError(404, "route_not_found", "route not found")
                payload = self.read_json(2048)
                username = payload.get("username")
                password = payload.get("password")
                if not isinstance(username, str) or not isinstance(password, str):
                    raise ApiError(400, "invalid_credentials", "用户名和密码不能为空")
                user = self.auth_store.authenticate(username, password)
                if user is None:
                    raise ApiError(401, "invalid_credentials", "用户名或密码错误")
                token, expires_at = self.auth_store.create_session(user["id"])
                cookie = (
                    f"{SESSION_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; "
                    f"Max-Age={max(0, expires_at - int(time.time()))}"
                )
                self.send_payload(200, {"user": user, "expiresAt": expires_at}, {"Set-Cookie": cookie})
                return
            if request_path == "/api/auth/logout":
                if self.auth_store is not None:
                    token = self.session_token()
                    if token:
                        self.auth_store.revoke_session(token)
                self.send_payload(
                    200,
                    {"status": "logged_out"},
                    {"Set-Cookie": f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"},
                )
                return
            user = self.require_user()
            if request_path == WORKSPACE_COLLECTION_ROUTE:
                if user is None:
                    raise ApiError(404, "route_not_found", "route not found")
                self.send_payload(201, self.control.create_workspace(user["id"], self.read_json()))
                return
            workspace_match = WORKSPACE_ROUTE_RE.fullmatch(request_path)
            if workspace_match:
                if user is None:
                    raise ApiError(404, "route_not_found", "route not found")
                workspace_id, action = workspace_match.groups()
                if action == "select":
                    self.send_payload(200, self.control.select_workspace(user["id"], workspace_id))
                elif action == "conversations":
                    self.send_payload(
                        201,
                        self.control.create_workspace_conversation(user["id"], workspace_id),
                    )
                else:
                    raise ApiError(404, "route_not_found", "route not found")
                return
            if request_path == "/api/instances":
                self.send_payload(
                    201,
                    self.control.create(self.read_json(), user["id"] if user is not None else None),
                )
                return
            conversation_collection = CONVERSATION_COLLECTION_ROUTE_RE.fullmatch(request_path)
            if conversation_collection:
                self.authorize_instance(conversation_collection.group(1), user)
                self.send_payload(
                    201,
                    self.control.create_conversation(
                        conversation_collection.group(1), self.read_json()
                    ),
                )
                return
            conversation_match = CONVERSATION_ROUTE_RE.fullmatch(request_path)
            if conversation_match and conversation_match.group(3):
                instance_id, conversation_id, _ = conversation_match.groups()
                self.authorize_instance(instance_id, user)
                self.send_payload(
                    202,
                    self.control.create_conversation_message(
                        instance_id,
                        conversation_id,
                        self.read_json(),
                        user["id"] if user is not None else None,
                    ),
                )
                return
            task_match = TASK_COLLECTION_ROUTE_RE.fullmatch(request_path)
            if task_match:
                self.authorize_instance(task_match.group(1), user)
                self.send_payload(202, self.control.create_task(task_match.group(1), self.read_json()))
                return
            match = INSTANCE_ROUTE_RE.fullmatch(request_path)
            if not match or match.group(2) is None:
                raise ApiError(404, "route_not_found", "route not found")
            instance_id, action = match.groups()
            self.authorize_instance(instance_id, user)
            if action == "start":
                self.send_payload(
                    200, self.control.start(instance_id, user["id"] if user is not None else None)
                )
            elif action == "stop":
                self.send_payload(200, self.control.stop(instance_id))
            elif action == "heartbeat":
                self.send_payload(200, self.control.heartbeat(instance_id))
            else:
                raise ApiError(404, "route_not_found", "route not found")
        except ApiError as error:
            self.send_error_payload(error)

    def do_DELETE(self) -> None:
        """处理工作区、会话和旧式实例记录删除。"""
        try:
            user = self.require_user()
            request_path = urlsplit(self.path).path
            conversation_match = WORKSPACE_CONVERSATION_ROUTE_RE.fullmatch(request_path)
            if conversation_match:
                if user is None:
                    raise ApiError(404, "route_not_found", "route not found")
                workspace_id, conversation_id = conversation_match.groups()
                self.send_payload(
                    200,
                    self.control.delete_workspace_conversation(
                        user["id"], workspace_id, conversation_id
                    ),
                )
                return
            workspace_match = WORKSPACE_ROUTE_RE.fullmatch(request_path)
            if workspace_match and workspace_match.group(2) is None:
                if user is None:
                    raise ApiError(404, "route_not_found", "route not found")
                self.send_payload(
                    200,
                    self.control.delete_workspace(user["id"], workspace_match.group(1)),
                )
                return
            match = INSTANCE_ROUTE_RE.fullmatch(request_path)
            if not match or match.group(2) is not None:
                raise ApiError(404, "route_not_found", "route not found")
            self.authorize_instance(match.group(1), user)
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
    parser.add_argument(
        "--task-database",
        type=Path,
        help="legacy tasks.db to import once into --database",
    )
    parser.add_argument("--deepseek-credential", type=Path)
    parser.add_argument("--idle-timeout", type=int, default=300)
    parser.add_argument(
        "--require-auth",
        action="store_true",
        help="require M16 local-account sessions for every instance API",
    )
    return parser


def main() -> None:
    """验证安全启动条件并运行多线程 loopback HTTP 服务。"""
    args = build_parser().parse_args()
    if os.geteuid() != 0:
        raise SystemExit("control plane must run as root")
    if args.listen_host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("control plane only supports loopback listeners")
    if args.idle_timeout < 1:
        raise SystemExit("idle timeout must be at least one second")

    store = InstanceStore(args.database)
    auth_store = AuthStore(args.database) if args.require_auth else None
    if auth_store is not None and not auth_store.has_users():
        raise SystemExit("no M16 user exists; run configure-user before starting the control plane")
    tasks = TaskStore(args.database, legacy_database_path=args.task_database)
    workspaces = WorkspaceStore(args.database)
    runtime = InstanceRuntime(
        state_root=args.state_root,
        idle_timeout_seconds=args.idle_timeout,
        deepseek_credential=args.deepseek_credential,
        state_database=args.database,
    )
    control = ControlPlane(store, runtime, tasks, PiGateway(), workspaces)
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), ControlPlaneHandler)
    server.control = control  # type: ignore[attr-defined]
    server.auth_store = auth_store  # type: ignore[attr-defined]
    print(
        f"CONTROL_PLANE_READY bind={args.listen_host}:{args.listen_port} "
        f"idle_timeout={args.idle_timeout}s auth={'session-cookie' if auth_store else 'disabled'}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
