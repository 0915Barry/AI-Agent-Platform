#!/usr/bin/env python3
"""生命周期显式停止与 watcher 协调的单元测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lifecycle


class FakeRuntimeStore:
    def __init__(self, metadata: dict) -> None:
        self.metadata = dict(metadata)
        self.saved_statuses: list[str] = []

    def load_legacy(self, _legacy_path: Path, _instance_id: str) -> dict:
        return dict(self.metadata)

    def save(self, metadata: dict) -> dict:
        self.metadata = dict(metadata)
        self.saved_statuses.append(str(metadata["status"]))
        return dict(metadata)


class LifecycleStopTests(unittest.TestCase):
    def test_stop_uses_intent_marker_without_persisting_stopping_status(self) -> None:
        metadata = {
            "id": "agent-test",
            "pid": 123,
            "processStartTicks": 456,
            "status": "running",
            "jailPath": "/srv/jailer/firecracker/agent-test",
            "volumePath": "/srv/fc/volumes/users/user-test.ext4",
            "networkDatabasePath": "",
            "networkLeasePath": "",
            "tapName": "",
            "nftFamily": "inet",
            "nftTable": "",
            "sidecars": [],
        }
        store = FakeRuntimeStore(metadata)

        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary)
            marker = lifecycle.stop_intent_path(state_root, "agent-test")

            def observe_marker(_metadata: dict, *, guest_shutdown: bool = True) -> str:
                self.assertTrue(guest_shutdown)
                self.assertTrue(marker.is_file())
                return "sigterm"

            with (
                mock.patch.object(lifecycle, "stop_process", side_effect=observe_marker),
                mock.patch.object(lifecycle, "stop_sidecars"),
                mock.patch.object(lifecycle, "cleanup_ephemeral"),
            ):
                stopped = lifecycle.stop_instance(
                    store,
                    state_root,
                    "agent-test",
                    "api",
                )

        self.assertEqual(store.saved_statuses, ["stopped"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
