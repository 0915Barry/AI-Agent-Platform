#!/usr/bin/env python3
"""预热池 ``prewarm_items`` 状态机的 SQLite 存储。

阶段 1：纯数据库状态机 + 原子领取 + 条件转换。不启动 VMM、不接 Control Plane。
只负责把并发正确性这一层做对：

- 原子领取：``BEGIN IMMEDIATE`` 下 ``ready → claiming`` 只允许一个事务成功；
- 条件转换：``transition_if_current`` 同时匹配 ``status + operation_id``，旧代次
  无法更新新代次的状态；
- 唯一约束：一个 ``logical_instance_id`` 至多绑定一个未结束（claiming/assigned）预热项。
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

PREWARM_STATUSES = (
    "preparing",
    "ready",
    "claiming",
    "assigned",
    "draining",
    "error",
    "retired",
)


class PrewarmStoreError(RuntimeError):
    """预热池记录不存在或违反约束。"""


class PrewarmStore:
    """预热池状态存储，与 RuntimeStore 共用同一份 SQLite 数据库文件。"""

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
                CREATE TABLE IF NOT EXISTS prewarm_items (
                    runtime_id TEXT PRIMARY KEY,
                    host_id TEXT NOT NULL DEFAULT '',
                    profile_key TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'preparing','ready','claiming','assigned','draining','error','retired'
                    )),
                    claim_token_hash TEXT,
                    logical_instance_id TEXT,
                    user_id TEXT,
                    volume_path TEXT,
                    operation_id TEXT,
                    activation_id TEXT,
                    vmm_pid INTEGER,
                    process_start_ticks INTEGER,
                    created_at INTEGER NOT NULL,
                    ready_at INTEGER,
                    claimed_at INTEGER,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    last_health_at INTEGER,
                    last_error TEXT
                )
                """
            )
            # 一个逻辑实例至多绑定一个未结束预热项：claiming 与 assigned 都视为
            # 「正在使用」，二者合计只能出现一条。
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS prewarm_items_one_active_binding
                ON prewarm_items(logical_instance_id)
                WHERE logical_instance_id IS NOT NULL AND status IN ('claiming','assigned')
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS prewarm_items_status "
                "ON prewarm_items(status,ready_at)"
            )
        os.chmod(self.database_path, 0o600)

    @staticmethod
    def _to_dict(row: sqlite3.Row | None) -> dict | None:
        return None if row is None else dict(row)

    def create_item(
        self,
        runtime_id: str,
        profile_key: str,
        *,
        host_id: str = "",
        claim_token_hash: str | None = None,
        vmm_pid: int | None = None,
        process_start_ticks: int | None = None,
    ) -> dict:
        """写入一条 ``preparing`` 项。违反唯一约束时抛 PrewarmStoreError。"""
        now = int(time.time())
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO prewarm_items(
                        runtime_id,host_id,profile_key,status,claim_token_hash,
                        vmm_pid,process_start_ticks,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        runtime_id,
                        host_id,
                        profile_key,
                        "preparing",
                        claim_token_hash,
                        vmm_pid,
                        process_start_ticks,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise PrewarmStoreError(
                f"prewarm item already exists or violates a constraint: {runtime_id}"
            ) from error
        stored = self.get(runtime_id)
        if stored is None:
            raise PrewarmStoreError(f"prewarm item disappeared after insert: {runtime_id}")
        return stored

    def get(self, runtime_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM prewarm_items WHERE runtime_id=?", (runtime_id,)
            ).fetchone()
        return self._to_dict(row)

    def list_ready(self, profile_key: str | None = None) -> list[dict]:
        """列出可领取项；profile_key 为空时不按规格过滤。"""
        with self.connect() as connection:
            if profile_key is None:
                rows = connection.execute(
                    "SELECT * FROM prewarm_items WHERE status='ready' ORDER BY ready_at,runtime_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM prewarm_items WHERE status='ready' AND profile_key=? "
                    "ORDER BY ready_at,runtime_id",
                    (profile_key,),
                ).fetchall()
        return [dict(row) for row in rows]

    def mark_ready(
        self,
        runtime_id: str,
        *,
        ttl_seconds: int,
        claim_token_hash: str | None = None,
    ) -> bool:
        """``preparing → ready``，由 Worker 在收到 WARM_READY 后调用。"""
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE prewarm_items
                SET status='ready', claim_token_hash=?, ready_at=?, expires_at=?,
                    updated_at=?, last_error=NULL
                WHERE runtime_id=? AND status='preparing'
                """,
                (claim_token_hash, now, now + ttl_seconds, now, runtime_id),
            )
        return cursor.rowcount == 1

    def claim(
        self,
        profile_key: str,
        logical_instance_id: str,
        user_id: str,
        volume_path: str,
        operation_id: str,
        activation_id: str,
    ) -> dict | None:
        """原子领取一台 ``ready`` 项：``ready → claiming``。

        在 ``BEGIN IMMEDIATE`` 事务内完成「选择 + 更新」，并发请求只有一个能
        抢到同一项，其余返回 ``None`` 并走冷启动兜底。
        """
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT runtime_id FROM prewarm_items
                WHERE status='ready' AND profile_key=?
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY ready_at,runtime_id
                LIMIT 1
                """,
                (profile_key, now),
            ).fetchone()
            if row is None:
                return None
            runtime_id = row["runtime_id"]
            try:
                cursor = connection.execute(
                    """
                    UPDATE prewarm_items
                    SET status='claiming', logical_instance_id=?, user_id=?, volume_path=?,
                        operation_id=?, activation_id=?, claimed_at=?, updated_at=?
                    WHERE runtime_id=? AND status='ready'
                    """,
                    (
                        logical_instance_id,
                        user_id,
                        volume_path,
                        operation_id,
                        activation_id,
                        now,
                        now,
                        runtime_id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise PrewarmStoreError(
                    f"logical instance {logical_instance_id} already has an active prewarm binding"
                ) from error
            if cursor.rowcount != 1:
                return None
        return self.get(runtime_id)

    def transition_if_current(
        self,
        runtime_id: str,
        expected_status: str,
        operation_id: str,
        new_status: str,
    ) -> bool:
        """条件转换：仅当记录仍处于 ``expected_status`` 且属于本 ``operation_id`` 时更新。

        返回 ``True`` 表示本代操作完成转换；``False`` 表示记录已被其他代次修改。
        """
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE prewarm_items
                SET status=?, updated_at=?, last_error=NULL
                WHERE runtime_id=? AND status=? AND operation_id=?
                """,
                (new_status, now, runtime_id, expected_status, operation_id),
            )
        return cursor.rowcount == 1

    def mark_assigned(self, runtime_id: str, operation_id: str) -> bool:
        """``claiming → assigned``：Host 确认绑定并 COMMIT 后调用。"""
        return self.transition_if_current(runtime_id, "claiming", operation_id, "assigned")

    def mark_error(self, runtime_id: str, operation_id: str, error: str) -> bool:
        """``claiming → error``：领取或激活失败后调用，保留 last_error。"""
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE prewarm_items
                SET status='error', updated_at=?, last_error=?
                WHERE runtime_id=? AND status='claiming' AND operation_id=?
                """,
                (now, error, runtime_id, operation_id),
            )
        return cursor.rowcount == 1

    def mark_draining(self, runtime_id: str) -> bool:
        """``ready|error → draining``：Worker 回收或下线时调用。"""
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE prewarm_items
                SET status='draining', updated_at=?
                WHERE runtime_id=? AND status IN ('ready','error')
                """,
                (now, runtime_id),
            )
        return cursor.rowcount == 1

    def retire(self, runtime_id: str) -> bool:
        """``draining|error → retired``：物理资源已清理完成的终态。"""
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE prewarm_items
                SET status='retired', updated_at=?
                WHERE runtime_id=? AND status IN ('draining','error')
                """,
                (now, runtime_id),
            )
        return cursor.rowcount == 1

    def delete(self, runtime_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM prewarm_items WHERE runtime_id=?", (runtime_id,))
