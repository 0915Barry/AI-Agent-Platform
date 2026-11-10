#!/usr/bin/env python3
"""启动操作 ``startup_operations`` 代次存储。

阶段 1：只实现表与条件转换，用于审计与代次隔离。StartupCoordinator（阶段 2）
才真正编排 ``snapshot/prewarm/cold`` 策略并调用这里的 ``begin`` / ``transition``。

关键约束：同一 ``logical_instance_id`` 同时最多存在一个未结束操作
（``deciding/executing/committing/compensating``）；``succeeded/failed`` 是历史
终态，不阻止后续新操作。
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ACTIVE_STARTUP_STATUSES = ("deciding", "executing", "committing", "compensating")


class StartupStoreError(RuntimeError):
    """启动操作记录不存在或违反约束。"""


class StartupStore:
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
                CREATE TABLE IF NOT EXISTS startup_operations (
                    operation_id TEXT PRIMARY KEY,
                    logical_instance_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    strategy TEXT NOT NULL CHECK(strategy IN ('snapshot','prewarm','cold')),
                    status TEXT NOT NULL CHECK(status IN (
                        'deciding','executing','committing','succeeded','failed','compensating'
                    )),
                    runtime_id TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_error TEXT
                )
                """
            )
            # 一个逻辑实例至多一个未结束操作；终态 succeeded/failed 不在索引内。
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS startup_operations_one_active
                ON startup_operations(logical_instance_id)
                WHERE status IN ('deciding','executing','committing','compensating')
                """
            )
        os.chmod(self.database_path, 0o600)

    @staticmethod
    def _to_dict(row: sqlite3.Row | None) -> dict | None:
        return None if row is None else dict(row)

    def begin(
        self,
        operation_id: str,
        logical_instance_id: str,
        owner_id: str,
        strategy: str,
    ) -> dict:
        """登记一次启动操作（deciding）；该逻辑实例已有未结束操作时抛错。"""
        now = int(time.time())
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO startup_operations(
                        operation_id,logical_instance_id,owner_id,strategy,status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (operation_id, logical_instance_id, owner_id, strategy, "deciding", now, now),
                )
        except sqlite3.IntegrityError as error:
            raise StartupStoreError(
                f"logical instance {logical_instance_id} already has an active startup operation"
            ) from error
        stored = self.get(operation_id)
        if stored is None:
            raise StartupStoreError(f"startup operation disappeared after begin: {operation_id}")
        return stored

    def get(self, operation_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM startup_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        return self._to_dict(row)

    def get_active(self, logical_instance_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM startup_operations
                WHERE logical_instance_id=? AND status IN (
                    'deciding','executing','committing','compensating'
                )
                """,
                (logical_instance_id,),
            ).fetchone()
        return self._to_dict(row)

    def transition(
        self,
        operation_id: str,
        expected_status: str,
        new_status: str,
        *,
        runtime_id: str | None = None,
        error: str | None = None,
    ) -> bool:
        """条件转换：仅当 operation_id 与 status 都匹配时更新。"""
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE startup_operations
                SET status=?, runtime_id=COALESCE(?,runtime_id), updated_at=?, last_error=?
                WHERE operation_id=? AND status=?
                """,
                (new_status, runtime_id, now, error, operation_id, expected_status),
            )
        return cursor.rowcount == 1

    def mark_succeeded(self, operation_id: str) -> bool:
        return self.transition(operation_id, "committing", "succeeded")

    def mark_failed(self, operation_id: str, error: str) -> bool:
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE startup_operations
                SET status='failed', updated_at=?, last_error=?
                WHERE operation_id=? AND status IN (
                    'deciding','executing','committing','compensating'
                )
                """,
                (now, error, operation_id),
            )
        return cursor.rowcount == 1
