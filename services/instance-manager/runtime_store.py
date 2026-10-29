#!/usr/bin/env python3
"""Firecracker 实例运行元数据的 SQLite 存储。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class RuntimeStoreError(RuntimeError):
    """运行元数据不存在或无法持久化。"""


class UserVolumeStore:
    """记录每个用户唯一的持久化数据盘。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_volumes (
                    user_id TEXT PRIMARY KEY,
                    volume_path TEXT UNIQUE NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
        os.chmod(self.database_path, 0o600)

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

    def get(self, user_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT user_id,volume_path,created_at,updated_at FROM user_volumes WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def ensure(self, user_id: str, volume_path: Path) -> dict:
        """首次启动时建立归属；后续启动必须继续使用同一块盘。"""

        now = int(time.time())
        resolved_path = str(volume_path.resolve())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT user_id,volume_path,created_at,updated_at FROM user_volumes WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if existing is not None:
                if existing["volume_path"] != resolved_path:
                    raise RuntimeStoreError(
                        f"user volume path mismatch for {user_id}: {existing['volume_path']}"
                    )
                return dict(existing)
            connection.execute(
                "INSERT INTO user_volumes(user_id,volume_path,created_at,updated_at) VALUES(?,?,?,?)",
                (user_id, resolved_path, now, now),
            )
        stored = self.get(user_id)
        if stored is None:
            raise RuntimeStoreError("user volume binding disappeared after save")
        return stored


class RuntimeStore:
    """保存 VMM 进程、jail、数据盘和网络端点等实际运行状态。"""

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
                CREATE TABLE IF NOT EXISTS runtime_instances (
                    instance_id TEXT PRIMARY KEY,
                    api_socket TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    idle_timeout_seconds INTEGER NOT NULL,
                    jail_path TEXT NOT NULL,
                    nft_family TEXT NOT NULL,
                    nft_table TEXT NOT NULL,
                    network_database_path TEXT NOT NULL,
                    network_lease_path TEXT NOT NULL,
                    network_subnet TEXT NOT NULL,
                    host_ip TEXT NOT NULL,
                    guest_ip TEXT NOT NULL,
                    rpc_port INTEGER NOT NULL,
                    pid INTEGER NOT NULL,
                    process_start_ticks INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('running','stopped')),
                    tap_name TEXT NOT NULL,
                    volume_path TEXT NOT NULL,
                    stop_reason TEXT,
                    stop_method TEXT,
                    stopped_at INTEGER
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_sidecars (
                    instance_id TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    process_start_ticks INTEGER NOT NULL,
                    PRIMARY KEY(instance_id,pid),
                    FOREIGN KEY(instance_id) REFERENCES runtime_instances(instance_id)
                        ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS runtime_instances_status "
                "ON runtime_instances(status,created_at)"
            )
        os.chmod(self.database_path, 0o600)

    @staticmethod
    def _to_metadata(row: sqlite3.Row, sidecars: list[sqlite3.Row]) -> dict:
        metadata = {
            "apiSocket": row["api_socket"],
            "createdAt": row["created_at"],
            "id": row["instance_id"],
            "idleTimeoutSeconds": row["idle_timeout_seconds"],
            "jailPath": row["jail_path"],
            "nftFamily": row["nft_family"],
            "nftTable": row["nft_table"],
            "networkDatabasePath": row["network_database_path"],
            "networkLeasePath": row["network_lease_path"],
            "networkSubnet": row["network_subnet"],
            "hostIp": row["host_ip"],
            "guestIp": row["guest_ip"],
            "rpcPort": row["rpc_port"],
            "pid": row["pid"],
            "processStartTicks": row["process_start_ticks"],
            "status": row["status"],
            "sidecars": [
                {"pid": sidecar["pid"], "processStartTicks": sidecar["process_start_ticks"]}
                for sidecar in sidecars
            ],
            "tapName": row["tap_name"],
            "volumePath": row["volume_path"],
        }
        if row["stop_reason"] is not None:
            metadata["stopReason"] = row["stop_reason"]
        if row["stop_method"] is not None:
            metadata["stopMethod"] = row["stop_method"]
        if row["stopped_at"] is not None:
            metadata["stoppedAt"] = row["stopped_at"]
        return metadata

    def get(self, instance_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_instances WHERE instance_id=?", (instance_id,)
            ).fetchone()
            if row is None:
                return None
            sidecars = connection.execute(
                "SELECT pid,process_start_ticks FROM runtime_sidecars "
                "WHERE instance_id=? ORDER BY pid",
                (instance_id,),
            ).fetchall()
        return self._to_metadata(row, sidecars)

    def save(self, metadata: dict) -> dict:
        """原子替换实例运行记录和其 sidecar 进程列表。"""
        values = (
            str(metadata["id"]),
            str(metadata["apiSocket"]),
            int(metadata["createdAt"]),
            int(metadata["idleTimeoutSeconds"]),
            str(metadata["jailPath"]),
            str(metadata.get("nftFamily", "")),
            str(metadata.get("nftTable", "")),
            str(metadata.get("networkDatabasePath", "")),
            str(metadata.get("networkLeasePath", "")),
            str(metadata.get("networkSubnet", "")),
            str(metadata.get("hostIp", "")),
            str(metadata.get("guestIp", "")),
            int(metadata.get("rpcPort", 0)),
            int(metadata["pid"]),
            int(metadata["processStartTicks"]),
            str(metadata["status"]),
            str(metadata.get("tapName", "")),
            str(metadata["volumePath"]),
            metadata.get("stopReason"),
            metadata.get("stopMethod"),
            metadata.get("stoppedAt"),
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO runtime_instances(
                    instance_id,api_socket,created_at,idle_timeout_seconds,jail_path,
                    nft_family,nft_table,network_database_path,network_lease_path,
                    network_subnet,host_ip,guest_ip,rpc_port,pid,process_start_ticks,
                    status,tap_name,volume_path,stop_reason,stop_method,stopped_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    api_socket=excluded.api_socket,
                    created_at=excluded.created_at,
                    idle_timeout_seconds=excluded.idle_timeout_seconds,
                    jail_path=excluded.jail_path,
                    nft_family=excluded.nft_family,
                    nft_table=excluded.nft_table,
                    network_database_path=excluded.network_database_path,
                    network_lease_path=excluded.network_lease_path,
                    network_subnet=excluded.network_subnet,
                    host_ip=excluded.host_ip,
                    guest_ip=excluded.guest_ip,
                    rpc_port=excluded.rpc_port,
                    pid=excluded.pid,
                    process_start_ticks=excluded.process_start_ticks,
                    status=excluded.status,
                    tap_name=excluded.tap_name,
                    volume_path=excluded.volume_path,
                    stop_reason=excluded.stop_reason,
                    stop_method=excluded.stop_method,
                    stopped_at=excluded.stopped_at
                """,
                values,
            )
            connection.execute(
                "DELETE FROM runtime_sidecars WHERE instance_id=?", (metadata["id"],)
            )
            connection.executemany(
                "INSERT INTO runtime_sidecars(instance_id,pid,process_start_ticks) VALUES(?,?,?)",
                [
                    (metadata["id"], int(sidecar["pid"]), int(sidecar["processStartTicks"]))
                    for sidecar in metadata.get("sidecars", [])
                ],
            )
        stored = self.get(str(metadata["id"]))
        if stored is None:
            raise RuntimeStoreError("runtime metadata disappeared after save")
        return stored

    def delete(self, instance_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM runtime_instances WHERE instance_id=?", (instance_id,))

    def load_legacy(self, legacy_path: Path, instance_id: str) -> dict | None:
        """首次升级时把旧 JSON 元数据导入 SQLite，成功后删除旧文件。"""
        stored = self.get(instance_id)
        if stored is not None:
            return stored
        try:
            payload = json.loads(legacy_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeStoreError(f"invalid legacy runtime metadata: {legacy_path}") from error
        if payload.get("id") != instance_id:
            raise RuntimeStoreError(f"legacy runtime metadata ID mismatch: {legacy_path}")
        stored = self.save(payload)
        legacy_path.unlink(missing_ok=True)
        return stored
