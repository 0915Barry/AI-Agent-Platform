#!/usr/bin/env python3
"""用户工作区及当前选择状态的 SQLite 存储。"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class WorkspaceStoreError(RuntimeError):
    """工作区不存在、名称冲突或归属不匹配。"""


class WorkspaceStore:
    """维护用户与工作区的一对多关系，以及当前工作区选择。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
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
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL COLLATE NOCASE,
                    directory_name TEXT UNIQUE NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(user_id,name)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_workspace_preferences (
                    user_id TEXT PRIMARY KEY,
                    current_workspace_id TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(current_workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS workspaces_user_updated "
                "ON workspaces(user_id,updated_at DESC,id)"
            )

    @staticmethod
    def validate_name(name: str) -> str:
        normalized = " ".join(name.strip().split())
        if not normalized:
            raise WorkspaceStoreError("工作区名称不能为空")
        if len(normalized.encode("utf-8")) > 120:
            raise WorkspaceStoreError("工作区名称不能超过 120 个 UTF-8 字节")
        return normalized

    @staticmethod
    def from_row(row: sqlite3.Row, current_workspace_id: str | None = None) -> dict[str, Any]:
        keys = set(row.keys())
        updated_at = row["updated_at"]
        if "last_activity_at" in keys and row["last_activity_at"] is not None:
            updated_at = max(updated_at, row["last_activity_at"])
        result = {
            "id": row["id"],
            "name": row["name"],
            "directoryName": row["directory_name"],
            "createdAt": row["created_at"],
            "updatedAt": updated_at,
            "current": row["id"] == current_workspace_id,
        }
        if "conversation_count" in keys:
            result["conversationCount"] = row["conversation_count"]
        return result

    def create(self, user_id: str, name: str, *, select: bool = True) -> dict[str, Any]:
        name = self.validate_name(name)
        workspace_id = f"workspace-{uuid.uuid4().hex[:16]}"
        directory_name = workspace_id
        now = int(time.time())
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO workspaces(id,user_id,name,directory_name,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (workspace_id, user_id, name, directory_name, now, now),
                )
                if select:
                    connection.execute(
                        "INSERT INTO user_workspace_preferences(user_id,current_workspace_id,updated_at) "
                        "VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                        "current_workspace_id=excluded.current_workspace_id,updated_at=excluded.updated_at",
                        (user_id, workspace_id, now),
                    )
        except sqlite3.IntegrityError as error:
            raise WorkspaceStoreError(f"工作区名称已存在：{name}") from error
        return self.get(user_id, workspace_id)

    def ensure_default(self, user_id: str) -> dict[str, Any]:
        current = self.current(user_id)
        if current is not None:
            return current
        listed = self.list(user_id)
        if listed:
            return self.select(user_id, listed[0]["id"])
        return self.create(user_id, "默认工作区")

    def list(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            preference = connection.execute(
                "SELECT current_workspace_id FROM user_workspace_preferences WHERE user_id=?",
                (user_id,),
            ).fetchone()
            current_id = None if preference is None else preference["current_workspace_id"]
            has_conversations = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'"
            ).fetchone()
            if has_conversations is None:
                rows = connection.execute(
                    "SELECT * FROM workspaces WHERE user_id=? ORDER BY updated_at DESC,id",
                    (user_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT w.*,COUNT(c.id) AS conversation_count,
                           MAX(c.updated_at) AS last_activity_at
                    FROM workspaces w
                    LEFT JOIN conversations c ON c.workspace_id=w.id
                    WHERE w.user_id=?
                    GROUP BY w.id
                    ORDER BY CASE
                        WHEN COALESCE(MAX(c.updated_at),0) > w.updated_at
                        THEN COALESCE(MAX(c.updated_at),0)
                        ELSE w.updated_at
                    END DESC,w.id
                    """,
                    (user_id,),
                ).fetchall()
        return [self.from_row(row, current_id) for row in rows]

    def get(self, user_id: str, workspace_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE id=? AND user_id=?",
                (workspace_id, user_id),
            ).fetchone()
            preference = connection.execute(
                "SELECT current_workspace_id FROM user_workspace_preferences WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if row is None:
            raise WorkspaceStoreError(f"工作区不存在：{workspace_id}")
        current_id = None if preference is None else preference["current_workspace_id"]
        return self.from_row(row, current_id)

    def current(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT w.* FROM user_workspace_preferences p "
                "JOIN workspaces w ON w.id=p.current_workspace_id "
                "WHERE p.user_id=? AND w.user_id=?",
                (user_id, user_id),
            ).fetchone()
        return None if row is None else self.from_row(row, row["id"])

    def select(self, user_id: str, workspace_id: str) -> dict[str, Any]:
        workspace = self.get(user_id, workspace_id)
        now = int(time.time())
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO user_workspace_preferences(user_id,current_workspace_id,updated_at) "
                "VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "current_workspace_id=excluded.current_workspace_id,updated_at=excluded.updated_at",
                (user_id, workspace_id, now),
            )
        workspace["current"] = True
        return workspace

    def delete(self, user_id: str, workspace_id: str) -> dict[str, Any]:
        """删除用户工作区，并返回被删除的稳定元数据。"""

        workspace = self.get(user_id, workspace_id)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "DELETE FROM workspaces WHERE id=? AND user_id=?",
                (workspace_id, user_id),
            )
            if changed.rowcount != 1:
                raise WorkspaceStoreError(f"工作区不存在：{workspace_id}")
        return workspace
