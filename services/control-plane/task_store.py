#!/usr/bin/env python3
"""控制面与 guest bridge 共享的 SQLite 任务队列。

控制面负责创建和查询任务；每个实例的 bridge 只领取属于自己的排队任务并追加
事件。两边使用同一个 WAL 数据库，从而避免把用户提示词塞进内核命令行或临时
网络协议。数据库只位于 Ubuntu 宿主机，不会进入 microVM。
"""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class TaskStoreError(RuntimeError):
    """任务不存在、状态冲突或数据库内容损坏。"""


class TaskStore:
    """提供任务创建、原子领取、事件追加和查询。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
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
        """幂等创建任务和事件表；事件序号由 SQLite 自动生成。"""

        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    output TEXT,
                    error TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS tasks_instance_status ON tasks(instance_id,status,created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS task_events_task_sequence ON task_events(task_id,sequence)"
            )

    @staticmethod
    def task_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "instanceId": row["instance_id"],
            "prompt": row["prompt"],
            "status": row["status"],
            "output": row["output"],
            "error": row["error"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def create(self, instance_id: str, prompt: str) -> dict[str, Any]:
        """创建一个排队任务，并同时写入 queued 事件。"""

        task_id = f"task-{uuid.uuid4().hex[:16]}"
        now = int(time.time())
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO tasks(id,instance_id,prompt,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (task_id, instance_id, prompt, "queued", now, now),
            )
            connection.execute(
                "INSERT INTO task_events(task_id,event_type,payload,created_at) VALUES(?,?,?,?)",
                (task_id, "queued", "{}", now),
            )
        return self.get(instance_id, task_id)

    def get(self, instance_id: str, task_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=? AND instance_id=?",
                (task_id, instance_id),
            ).fetchone()
        if row is None:
            raise TaskStoreError(f"task not found: {task_id}")
        return self.task_from_row(row)

    def claim_next(self, instance_id: str) -> dict[str, Any] | None:
        """用 IMMEDIATE 事务原子领取最早的 queued 任务，避免重复执行。"""

        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tasks WHERE instance_id=? AND status='queued' ORDER BY created_at,id LIMIT 1",
                (instance_id,),
            ).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                "UPDATE tasks SET status='running',updated_at=? WHERE id=? AND status='queued'",
                (now, row["id"]),
            )
            if changed.rowcount != 1:
                return None
            connection.execute(
                "INSERT INTO task_events(task_id,event_type,payload,created_at) VALUES(?,?,?,?)",
                (row["id"], "started", "{}", now),
            )
        return self.get(instance_id, row["id"])

    def append_event(self, instance_id: str, task_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """追加 guest 事件，并在 completed/failed 时同步任务终态。"""

        if event_type not in {"text", "tool_call", "tool_result", "completed", "failed"}:
            raise TaskStoreError(f"unsupported task event: {event_type}")
        task = self.get(instance_id, task_id)
        if task["status"] not in {"running", "queued"}:
            raise TaskStoreError(f"task is already terminal: {task_id}")
        now = int(time.time())
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO task_events(task_id,event_type,payload,created_at) VALUES(?,?,?,?)",
                (task_id, event_type, encoded, now),
            )
            if event_type == "completed":
                output = payload.get("output")
                if not isinstance(output, str):
                    raise TaskStoreError("completed event requires string output")
                connection.execute(
                    "UPDATE tasks SET status='completed',output=?,error=NULL,updated_at=? WHERE id=?",
                    (output, now, task_id),
                )
            elif event_type == "failed":
                error = payload.get("error")
                if not isinstance(error, str):
                    raise TaskStoreError("failed event requires string error")
                connection.execute(
                    "UPDATE tasks SET status='failed',error=?,updated_at=? WHERE id=?",
                    (error[:4000], now, task_id),
                )
            else:
                connection.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now, task_id))

    def events(self, instance_id: str, task_id: str) -> list[dict[str, Any]]:
        """按稳定序号返回任务事件，供前端轮询；后续可直接映射成 SSE。"""

        self.get(instance_id, task_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT sequence,event_type,payload,created_at FROM task_events WHERE task_id=? ORDER BY sequence",
                (task_id,),
            ).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "type": row["event_type"],
                "data": json.loads(row["payload"]),
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def fail_running(self, instance_id: str, reason: str) -> None:
        """实例停止时把尚未结束的任务标记失败，防止永久显示 running。"""

        now = int(time.time())
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM tasks WHERE instance_id=? AND status='running'",
                (instance_id,),
            ).fetchall()
            for row in rows:
                payload = json.dumps({"error": reason}, separators=(",", ":"))
                connection.execute(
                    "INSERT INTO task_events(task_id,event_type,payload,created_at) VALUES(?,?,?,?)",
                    (row["id"], "failed", payload, now),
                )
                connection.execute(
                    "UPDATE tasks SET status='failed',error=?,updated_at=? WHERE id=?",
                    (reason, now, row["id"]),
                )

    def delete_instance(self, instance_id: str) -> None:
        """显式销毁实例时删除其任务正文和事件，避免遗留用户数据。"""

        with self.connect() as connection:
            task_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM tasks WHERE instance_id=?",
                    (instance_id,),
                ).fetchall()
            ]
            for task_id in task_ids:
                connection.execute("DELETE FROM task_events WHERE task_id=?", (task_id,))
            connection.execute("DELETE FROM tasks WHERE instance_id=?", (instance_id,))
