#!/usr/bin/env python3
"""阶段 4：预热 Worker 容量决策的纯本地测试。"""

import tempfile
import unittest
from pathlib import Path

from prewarm_config import PrewarmConfig
from prewarm_store import PrewarmStore
from prewarm_worker import PrewarmWorker


class PrewarmWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = PrewarmStore(Path(self.temporary.name) / "control-plane.db")

    def worker(self, **overrides: str) -> PrewarmWorker:
        values = {
            "PREWARM_ENABLED": "1",
            "PREWARM_TARGET_READY": "2",
            "PREWARM_MAX_TOTAL": "3",
            "PREWARM_BUILD_CONCURRENCY": "2",
            "PREWARM_MIN_AVAILABLE_MIB": "1024",
            "PREWARM_MIN_FREE_NETWORK_SLOTS": "2",
            **overrides,
        }
        return PrewarmWorker(
            PrewarmConfig.from_env(values),
            prewarm_store=self.store,
            profile_key="profile-a",
        )

    def preparing(self, runtime_id: str) -> None:
        self.store.create_item(runtime_id, "profile-a")

    def ready(self, runtime_id: str, *, ttl_seconds: int = 900) -> None:
        self.preparing(runtime_id)
        self.assertTrue(self.store.mark_ready(runtime_id, ttl_seconds=ttl_seconds))

    def claiming(self, runtime_id: str) -> None:
        self.ready(runtime_id)
        self.assertIsNotNone(
            self.store.claim(
                "profile-a",
                f"logical-{runtime_id}",
                f"user-{runtime_id}",
                f"/vol/{runtime_id}.ext4",
                f"operation-{runtime_id}",
                f"activation-{runtime_id}",
            )
        )

    def test_empty_pool_plans_target_within_all_limits(self) -> None:
        plan = self.worker().plan_fill(available_mib=4096, free_network_slots=10)
        self.assertEqual(plan.create_count, 2)
        self.assertEqual(plan.reason, "fill")

    def test_ready_and_preparing_satisfy_target(self) -> None:
        self.ready("ready-1")
        self.preparing("building-1")
        plan = self.worker().plan_fill(available_mib=4096, free_network_slots=10)
        self.assertEqual(plan.create_count, 0)
        self.assertEqual(plan.reason, "target_satisfied")

    def test_claiming_triggers_replacement_but_counts_toward_max(self) -> None:
        self.claiming("claimed-1")
        plan = self.worker().plan_fill(available_mib=4096, free_network_slots=10)
        self.assertEqual(plan.create_count, 2)
        self.assertEqual(plan.claiming, 1)
        self.assertEqual(plan.total_public, 1)

    def test_expired_ready_does_not_satisfy_target_or_bypass_max(self) -> None:
        self.ready("expired-1", ttl_seconds=-1)
        plan = self.worker(
            PREWARM_TARGET_READY="1",
            PREWARM_MAX_TOTAL="1",
        ).plan_fill(available_mib=4096, free_network_slots=10)
        self.assertEqual(plan.claimable_ready, 0)
        self.assertEqual(plan.total_public, 1)
        self.assertEqual(plan.create_count, 0)
        self.assertEqual(plan.reason, "max_total")

    def test_build_concurrency_limits_creation(self) -> None:
        self.preparing("building-1")
        plan = self.worker(
            PREWARM_TARGET_READY="3",
            PREWARM_MAX_TOTAL="4",
            PREWARM_BUILD_CONCURRENCY="2",
        ).plan_fill(available_mib=4096, free_network_slots=10)
        self.assertEqual(plan.create_count, 1)

    def test_network_reserve_limits_creation(self) -> None:
        plan = self.worker().plan_fill(available_mib=4096, free_network_slots=3)
        self.assertEqual(plan.create_count, 1)
        self.assertEqual(plan.reason, "fill")

    def test_network_reserve_can_pause_fill(self) -> None:
        plan = self.worker().plan_fill(available_mib=4096, free_network_slots=2)
        self.assertEqual(plan.create_count, 0)
        self.assertEqual(plan.reason, "network_guard")

    def test_memory_guard_pauses_fill(self) -> None:
        plan = self.worker().plan_fill(available_mib=1023, free_network_slots=10)
        self.assertEqual(plan.create_count, 0)
        self.assertEqual(plan.reason, "memory_guard")

    def test_disabled_worker_never_creates(self) -> None:
        plan = self.worker(PREWARM_ENABLED="0").plan_fill(
            available_mib=4096,
            free_network_slots=10,
        )
        self.assertEqual(plan.create_count, 0)
        self.assertEqual(plan.reason, "disabled")

    def test_other_profile_does_not_affect_plan(self) -> None:
        self.store.create_item("other-1", "profile-b")
        self.assertTrue(self.store.mark_ready("other-1", ttl_seconds=900))
        plan = self.worker().plan_fill(available_mib=4096, free_network_slots=10)
        self.assertEqual(plan.create_count, 2)


if __name__ == "__main__":
    unittest.main()
