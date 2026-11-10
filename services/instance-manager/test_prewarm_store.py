#!/usr/bin/env python3
"""PrewarmStore 的本地 SQLite 单元测试（阶段 1）。"""

import tempfile
import threading
import unittest
from pathlib import Path

from prewarm_store import PrewarmStore, PrewarmStoreError


class PrewarmStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "control-plane.db"
        self.store = PrewarmStore(self.database)

    def _ready_item(self, runtime_id: str, profile: str = "profile-a") -> None:
        self.store.create_item(runtime_id, profile, claim_token_hash="tok")
        self.assertTrue(self.store.mark_ready(runtime_id, ttl_seconds=900))

    def test_create_and_get_item(self) -> None:
        item = self.store.create_item("rt-1", "profile-a", vmm_pid=123, process_start_ticks=456)
        self.assertEqual(item["status"], "preparing")
        self.assertEqual(item["vmm_pid"], 123)
        self.assertEqual(item["process_start_ticks"], 456)
        self.assertEqual(self.store.get("rt-1")["runtime_id"], "rt-1")

    def test_create_duplicate_runtime_id_rejected(self) -> None:
        self.store.create_item("rt-1", "profile-a")
        with self.assertRaises(PrewarmStoreError):
            self.store.create_item("rt-1", "profile-a")

    def test_mark_ready_sets_timestamps_and_ttl(self) -> None:
        self.store.create_item("rt-1", "profile-a")
        self.assertTrue(self.store.mark_ready("rt-1", ttl_seconds=900, claim_token_hash="tok"))
        item = self.store.get("rt-1")
        self.assertEqual(item["status"], "ready")
        self.assertIsNotNone(item["ready_at"])
        self.assertIsNotNone(item["expires_at"])
        self.assertEqual(item["claim_token_hash"], "tok")

    def test_claim_sets_binding_fields(self) -> None:
        self._ready_item("rt-1")
        claimed = self.store.claim(
            "profile-a", "logical-1", "user-1", "/vol/1.ext4", "op-1", "act-1"
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["status"], "claiming")
        self.assertEqual(claimed["logical_instance_id"], "logical-1")
        self.assertEqual(claimed["user_id"], "user-1")
        self.assertEqual(claimed["volume_path"], "/vol/1.ext4")
        self.assertEqual(claimed["operation_id"], "op-1")
        self.assertEqual(claimed["activation_id"], "act-1")
        self.assertIsNotNone(claimed["claimed_at"])

    def test_claim_returns_none_when_no_ready(self) -> None:
        self.assertIsNone(
            self.store.claim("profile-a", "logical-1", "user-1", "/vol/1.ext4", "op-1", "act-1")
        )

    def test_claim_skips_expired_item(self) -> None:
        self.store.create_item("rt-1", "profile-a")
        # ttl_seconds 为负会让 expires_at 落在过去。
        self.assertTrue(self.store.mark_ready("rt-1", ttl_seconds=-1))
        self.assertIsNone(
            self.store.claim("profile-a", "logical-1", "user-1", "/vol/1.ext4", "op-1", "act-1")
        )

    def test_claim_only_matches_profile(self) -> None:
        self._ready_item("rt-1", profile="profile-a")
        self.assertIsNone(
            self.store.claim("profile-b", "logical-1", "user-1", "/vol/1.ext4", "op-1", "act-1")
        )

    def test_transition_if_current_succeeds(self) -> None:
        self._ready_item("rt-1")
        self.store.claim("profile-a", "logical-1", "user-1", "/vol/1.ext4", "op-1", "act-1")
        self.assertTrue(self.store.mark_assigned("rt-1", "op-1"))
        self.assertEqual(self.store.get("rt-1")["status"], "assigned")

    def test_transition_rejects_stale_operation(self) -> None:
        self._ready_item("rt-1")
        self.store.claim("profile-a", "logical-1", "user-1", "/vol/1.ext4", "op-1", "act-1")
        self.assertFalse(self.store.mark_assigned("rt-1", "op-other"))
        self.assertEqual(self.store.get("rt-1")["status"], "claiming")

    def test_transition_rejects_wrong_status(self) -> None:
        self._ready_item("rt-1")
        self.store.claim("profile-a", "logical-1", "user-1", "/vol/1.ext4", "op-1", "act-1")
        # 已不在 ready，无法再次 ready→claiming。
        self.assertFalse(
            self.store.transition_if_current("rt-1", "ready", "op-1", "claiming")
        )

    def test_mark_error_records_error(self) -> None:
        self._ready_item("rt-1")
        self.store.claim("profile-a", "logical-1", "user-1", "/vol/1.ext4", "op-1", "act-1")
        self.assertTrue(self.store.mark_error("rt-1", "op-1", "activation timeout"))
        item = self.store.get("rt-1")
        self.assertEqual(item["status"], "error")
        self.assertEqual(item["last_error"], "activation timeout")

    def test_mark_draining_and_retire(self) -> None:
        self._ready_item("rt-1")
        self.assertTrue(self.store.mark_draining("rt-1"))
        self.assertEqual(self.store.get("rt-1")["status"], "draining")
        self.assertTrue(self.store.retire("rt-1"))
        self.assertEqual(self.store.get("rt-1")["status"], "retired")

    def test_duplicate_active_binding_rejected(self) -> None:
        self._ready_item("rt-1")
        self._ready_item("rt-2")
        self.store.claim("profile-a", "logical-1", "user-1", "/vol/1.ext4", "op-1", "act-1")
        with self.assertRaises(PrewarmStoreError):
            self.store.claim("profile-a", "logical-1", "user-2", "/vol/2.ext4", "op-2", "act-2")

    def test_concurrent_claim_only_one_wins(self) -> None:
        self._ready_item("rt-only")
        winners: list[str] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            store = PrewarmStore(self.database)
            claimed = store.claim(
                "profile-a",
                f"logical-{index}",
                f"user-{index}",
                f"/vol/{index}.ext4",
                f"op-{index}",
                f"act-{index}",
            )
            if claimed is not None:
                with lock:
                    winners.append(claimed["runtime_id"])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(winners, ["rt-only"])
        self.assertEqual(self.store.get("rt-only")["status"], "claiming")


if __name__ == "__main__":
    unittest.main()
