#!/usr/bin/env python3
"""RuntimeStore 的本地 SQLite 单元测试。"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from runtime_store import RuntimeStore, RuntimeStoreError, UserVolumeStore


def metadata(instance_id: str = "agent-one") -> dict:
    return {
        "apiSocket": f"/srv/jailer/firecracker/{instance_id}/root/firecracker.socket",
        "createdAt": 100,
        "id": instance_id,
        "idleTimeoutSeconds": 300,
        "jailPath": f"/srv/jailer/firecracker/{instance_id}",
        "nftFamily": "inet",
        "nftTable": "aap_00000000",
        "networkDatabasePath": "/var/lib/fc/control-plane.db",
        "networkLeasePath": "",
        "networkSubnet": "172.29.0.0/30",
        "hostIp": "172.29.0.1",
        "guestIp": "172.29.0.2",
        "rpcPort": 8080,
        "pid": 1234,
        "processStartTicks": 5678,
        "status": "running",
        "sidecars": [{"pid": 1233, "processStartTicks": 5677}],
        "tapName": "fc00000000",
        "volumePath": f"/srv/fc/volumes/instances/{instance_id}.ext4",
    }


class RuntimeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "control-plane.db"
        self.store = RuntimeStore(self.database)

    def test_save_update_and_delete_runtime_metadata(self) -> None:
        stored = self.store.save(metadata())
        self.assertEqual(stored["guestIp"], "172.29.0.2")
        self.assertEqual(stored["sidecars"][0]["pid"], 1233)

        stored.update(
            {
                "status": "stopped",
                "stopReason": "idle_timeout",
                "stopMethod": "sigterm",
                "stoppedAt": 200,
            }
        )
        stopped = self.store.save(stored)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["stopReason"], "idle_timeout")

        self.store.delete("agent-one")
        self.assertIsNone(self.store.get("agent-one"))
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runtime_sidecars").fetchone()[0], 0)

    def test_legacy_json_is_imported_once(self) -> None:
        legacy = Path(self.temporary.name) / "agent-old.json"
        legacy.write_text(json.dumps(metadata("agent-old")), encoding="utf-8")
        imported = self.store.load_legacy(legacy, "agent-old")
        self.assertIsNotNone(imported)
        self.assertFalse(legacy.exists())
        self.assertEqual(self.store.get("agent-old")["status"], "running")

    def test_user_has_one_stable_volume_binding(self) -> None:
        volumes = UserVolumeStore(self.database)
        first_path = Path(self.temporary.name) / "users" / "user-one.ext4"
        stored = volumes.ensure("user-one", first_path)
        self.assertEqual(stored["volume_path"], str(first_path.resolve()))
        self.assertEqual(volumes.ensure("user-one", first_path)["volume_path"], str(first_path.resolve()))

        with self.assertRaises(RuntimeStoreError):
            volumes.ensure(
                "user-one",
                Path(self.temporary.name) / "users" / "different.ext4",
            )


if __name__ == "__main__":
    unittest.main()
