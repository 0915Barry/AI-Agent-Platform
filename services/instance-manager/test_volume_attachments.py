#!/usr/bin/env python3
"""VolumeAttachmentStore 数据盘独占的本地单元测试（阶段 1）。"""

import tempfile
import unittest
from pathlib import Path

from runtime_store import RuntimeStoreError, VolumeAttachmentStore


class VolumeAttachmentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "control-plane.db"
        self.store = VolumeAttachmentStore(self.database)

    def _reserve(self, volume: str = "/vol/1.ext4", op: str = "op-1") -> dict:
        return self.store.reserve(volume, "user-1", "logical-1", "rt-1", op)

    def test_reserve_and_get(self) -> None:
        attachment = self._reserve()
        self.assertEqual(attachment["status"], "reserved")
        self.assertEqual(attachment["volume_path"], "/vol/1.ext4")
        self.assertEqual(self.store.get("/vol/1.ext4")["operation_id"], "op-1")

    def test_duplicate_reserve_rejected(self) -> None:
        self._reserve()
        with self.assertRaises(RuntimeStoreError):
            self._reserve(op="op-2")

    def test_reserve_then_attached(self) -> None:
        self._reserve()
        self.assertTrue(self.store.mark_attached("/vol/1.ext4", "op-1"))
        self.assertEqual(self.store.get("/vol/1.ext4")["status"], "attached")

    def test_suspend_and_resume(self) -> None:
        self._reserve()
        self.store.mark_attached("/vol/1.ext4", "op-1")
        self.assertTrue(self.store.mark_suspended("/vol/1.ext4", "op-1"))
        self.assertEqual(self.store.get("/vol/1.ext4")["status"], "suspended")
        self.assertTrue(self.store.mark_resumed("/vol/1.ext4", "op-1"))
        self.assertEqual(self.store.get("/vol/1.ext4")["status"], "attached")

    def test_transition_rejects_stale_operation(self) -> None:
        self._reserve(op="op-1")
        self.assertFalse(self.store.mark_attached("/vol/1.ext4", "op-other"))
        self.assertEqual(self.store.get("/vol/1.ext4")["status"], "reserved")

    def test_mark_error_keeps_lock(self) -> None:
        self._reserve(op="op-1")
        self.assertTrue(self.store.mark_error("/vol/1.ext4", "op-1", "mount state unknown"))
        attachment = self.store.get("/vol/1.ext4")
        self.assertEqual(attachment["status"], "error")
        self.assertEqual(attachment["last_error"], "mount state unknown")
        # error 保持独占，重新 reserve 仍被拒。
        with self.assertRaises(RuntimeStoreError):
            self._reserve(op="op-2")

    def test_release_allows_reserve_again(self) -> None:
        self._reserve(op="op-1")
        self.assertTrue(self.store.release("/vol/1.ext4", "op-1"))
        self.assertIsNone(self.store.get("/vol/1.ext4"))
        self._reserve(op="op-2")

    def test_release_rejects_stale_operation(self) -> None:
        self._reserve(op="op-1")
        self.assertFalse(self.store.release("/vol/1.ext4", "op-other"))
        self.assertIsNotNone(self.store.get("/vol/1.ext4"))


if __name__ == "__main__":
    unittest.main()
