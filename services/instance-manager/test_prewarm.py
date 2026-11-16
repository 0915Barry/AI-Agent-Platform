#!/usr/bin/env python3
"""预热池配置与阶段 3A 领取编排的纯本地单元测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prewarm_config import PrewarmConfig, PrewarmConfigError
from prewarm_manager import PrewarmManager, PrewarmManagerError
from prewarm_store import PrewarmStore
from runtime_store import RuntimeStore, VolumeAttachmentStore


class PrewarmConfigTests(unittest.TestCase):
    def test_defaults_are_disabled(self) -> None:
        config = PrewarmConfig.from_env({})
        self.assertFalse(config.enabled)
        self.assertEqual(config.target_ready, 1)
        self.assertEqual(config.max_total, 2)
        self.assertEqual(config.ttl_seconds, 900)

    def test_parses_enabled_values(self) -> None:
        self.assertTrue(PrewarmConfig.from_env({"PREWARM_ENABLED": "1"}).enabled)
        self.assertFalse(PrewarmConfig.from_env({"PREWARM_ENABLED": "0"}).enabled)
        self.assertTrue(PrewarmConfig.from_env({"PREWARM_ENABLED": "true"}).enabled)

    def test_rejects_invalid_int(self) -> None:
        with self.assertRaises(PrewarmConfigError):
            PrewarmConfig.from_env({"PREWARM_TARGET_READY": "abc"})

    def test_rejects_negative_target(self) -> None:
        with self.assertRaises(PrewarmConfigError):
            PrewarmConfig.from_env({"PREWARM_TARGET_READY": "-1"})

    def test_rejects_max_below_target(self) -> None:
        with self.assertRaises(PrewarmConfigError):
            PrewarmConfig.from_env({"PREWARM_TARGET_READY": "3", "PREWARM_MAX_TOTAL": "2"})

    def test_rejects_invalid_enabled(self) -> None:
        with self.assertRaises(PrewarmConfigError):
            PrewarmConfig.from_env({"PREWARM_ENABLED": "maybe"})

    def test_rejects_non_positive_timeout(self) -> None:
        with self.assertRaises(PrewarmConfigError):
            PrewarmConfig.from_env({"PREWARM_TTL_SECONDS": "0"})

    def test_parses_min_free_network_slots(self) -> None:
        config = PrewarmConfig.from_env({"PREWARM_MIN_FREE_NETWORK_SLOTS": "8"})
        self.assertEqual(config.min_free_network_slots, 8)

    def test_rejects_negative_min_free_network_slots(self) -> None:
        with self.assertRaises(PrewarmConfigError):
            PrewarmConfig.from_env({"PREWARM_MIN_FREE_NETWORK_SLOTS": "-1"})


class PrewarmManagerTests(unittest.TestCase):
    class FakeActivator:
        def __init__(self) -> None:
            self.fail = False
            self.fail_commit = False
            self.activated: list[tuple[str, str, str, str, str, str, str]] = []
            self.committed: list[tuple[str, str, str]] = []

        def activate_prewarmer(
            self,
            runtime_id: str,
            logical_instance_id: str,
            user_id: str,
            volume_path: str,
            operation_id: str,
            activation_id: str,
            claim_token_hash: str,
        ) -> dict:
            self.activated.append(
                (
                    runtime_id,
                    logical_instance_id,
                    user_id,
                    volume_path,
                    operation_id,
                    activation_id,
                    claim_token_hash,
                )
            )
            if self.fail:
                raise RuntimeError("activation failed")
            return {"id": runtime_id, "status": "running"}

        def commit_prewarmer(
            self, runtime_id: str, operation_id: str, activation_id: str
        ) -> None:
            if self.fail_commit:
                raise RuntimeError("commit failed")
            self.committed.append((runtime_id, operation_id, activation_id))

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "control-plane.db"
        self.prewarm_store = PrewarmStore(self.database)
        self.attachment_store = VolumeAttachmentStore(self.database)
        self.runtime_store = RuntimeStore(self.database)
        self.activator = self.FakeActivator()
        self.config = PrewarmConfig.from_env({"PREWARM_ENABLED": "1"})
        self.manager = PrewarmManager(
            self.config,
            prewarm_store=self.prewarm_store,
            attachment_store=self.attachment_store,
            runtime_store=self.runtime_store,
            activator=self.activator,
            new_activation_id=lambda: "activation-1",
        )

    def _ready(self, runtime_id: str = "runtime-1") -> None:
        self.prewarm_store.create_item(
            runtime_id, "profile-a", claim_token_hash="a" * 64
        )
        self.assertTrue(self.prewarm_store.mark_ready(runtime_id, ttl_seconds=900))

    def test_target_ready_reads_config(self) -> None:
        config = PrewarmConfig.from_env({"PREWARM_TARGET_READY": "5", "PREWARM_MAX_TOTAL": "5"})
        manager = PrewarmManager(config)
        self.assertEqual(manager.target_ready(), 5)

    def test_current_ready_reads_store(self) -> None:
        self._ready()
        self.assertEqual(self.manager.current_ready("profile-a"), 1)
        self.assertEqual(self.manager.current_ready("profile-b"), 0)

    def test_current_ready_excludes_expired_items(self) -> None:
        self.prewarm_store.create_item("runtime-expired", "profile-a")
        self.assertTrue(
            self.prewarm_store.mark_ready("runtime-expired", ttl_seconds=-1)
        )
        self.assertEqual(self.manager.current_ready("profile-a"), 0)

    def test_current_ready_without_store_is_zero(self) -> None:
        manager = PrewarmManager(PrewarmConfig.from_env({}))
        self.assertEqual(manager.current_ready(), 0)

    def test_automatic_pool_methods_remain_unimplemented(self) -> None:
        manager = PrewarmManager(PrewarmConfig.from_env({}))
        with self.assertRaises(NotImplementedError):
            manager.fill_if_needed()

    def test_disabled_claim_does_not_touch_store(self) -> None:
        self._ready()
        disabled = PrewarmManager(PrewarmConfig.from_env({}))
        result = disabled.claim(
            "profile-a", "logical-1", "user-1", "/vol/user-1.ext4", "operation-1"
        )
        self.assertFalse(result.claimed)
        self.assertEqual(result.reason, "disabled")
        self.assertEqual(self.prewarm_store.get("runtime-1")["status"], "ready")

    def test_no_ready_item_falls_back_without_side_effects(self) -> None:
        result = self.manager.claim(
            "profile-a", "logical-1", "user-1", "/vol/user-1.ext4", "operation-1"
        )
        self.assertFalse(result.claimed)
        self.assertEqual(result.reason, "unavailable")
        self.assertEqual(self.activator.activated, [])

    def test_claim_activate_and_commit(self) -> None:
        self._ready()
        result = self.manager.claim(
            "profile-a", "logical-1", "user-1", "/vol/user-1.ext4", "operation-1"
        )
        self.assertTrue(result.claimed)
        self.assertEqual(result.runtime_id, "runtime-1")
        self.assertEqual(result.activation_id, "activation-1")
        self.assertEqual(self.prewarm_store.get("runtime-1")["status"], "claiming")
        self.assertEqual(
            self.attachment_store.get("/vol/user-1.ext4")["status"], "attached"
        )
        self.assertEqual(self.runtime_store.resolve_runtime_id("logical-1"), "runtime-1")
        self.assertEqual(self.runtime_store.get_binding("logical-1")["status"], "activating")

        self.manager.commit(
            "logical-1", "runtime-1", "operation-1", "activation-1"
        )
        self.assertEqual(self.prewarm_store.get("runtime-1")["status"], "assigned")
        self.assertEqual(self.runtime_store.get_binding("logical-1")["status"], "running")
        self.assertEqual(
            self.activator.committed,
            [("runtime-1", "operation-1", "activation-1")],
        )

    def test_activation_failure_keeps_volume_locked(self) -> None:
        self._ready()
        self.activator.fail = True
        with self.assertRaisesRegex(PrewarmManagerError, "activation failed"):
            self.manager.claim(
                "profile-a", "logical-1", "user-1", "/vol/user-1.ext4", "operation-1"
            )
        self.assertEqual(self.prewarm_store.get("runtime-1")["status"], "error")
        attachment = self.attachment_store.get("/vol/user-1.ext4")
        self.assertEqual(attachment["status"], "error")
        self.assertEqual(attachment["operation_id"], "operation-1")

    def test_stale_commit_is_rejected(self) -> None:
        self._ready()
        self.manager.claim(
            "profile-a", "logical-1", "user-1", "/vol/user-1.ext4", "operation-1"
        )
        with self.assertRaisesRegex(PrewarmManagerError, "generation changed"):
            self.manager.commit(
                "logical-1", "runtime-1", "stale-operation", "activation-1"
            )

    def test_commit_failure_keeps_volume_locked_and_marks_binding_error(self) -> None:
        self._ready()
        self.manager.claim(
            "profile-a", "logical-1", "user-1", "/vol/user-1.ext4", "operation-1"
        )
        self.activator.fail_commit = True
        with self.assertRaisesRegex(PrewarmManagerError, "commit failed"):
            self.manager.commit(
                "logical-1", "runtime-1", "operation-1", "activation-1"
            )
        self.assertEqual(self.prewarm_store.get("runtime-1")["status"], "error")
        self.assertEqual(
            self.attachment_store.get("/vol/user-1.ext4")["status"], "error"
        )
        self.assertEqual(self.runtime_store.get_binding("logical-1")["status"], "error")

    def test_complete_stop_releases_attachment_binding_and_retires_item(self) -> None:
        self._ready()
        self.manager.claim(
            "profile-a", "logical-1", "user-1", "/vol/user-1.ext4", "operation-1"
        )
        self.manager.commit(
            "logical-1", "runtime-1", "operation-1", "activation-1"
        )
        with patch.object(
            self.runtime_store, "get", return_value={"status": "stopped"}
        ):
            self.manager.complete_stop("logical-1", "runtime-1", "operation-1")
        self.assertEqual(self.prewarm_store.get("runtime-1")["status"], "retired")
        self.assertIsNone(self.attachment_store.get("/vol/user-1.ext4"))
        self.assertIsNone(self.runtime_store.get_binding("logical-1"))

    def test_complete_stop_rejects_live_runtime(self) -> None:
        self._ready()
        self.manager.claim(
            "profile-a", "logical-1", "user-1", "/vol/user-1.ext4", "operation-1"
        )
        self.manager.commit(
            "logical-1", "runtime-1", "operation-1", "activation-1"
        )
        with patch.object(
            self.runtime_store, "get", return_value={"status": "running"}
        ):
            with self.assertRaisesRegex(PrewarmManagerError, "must be stopped"):
                self.manager.complete_stop("logical-1", "runtime-1", "operation-1")


if __name__ == "__main__":
    unittest.main()
