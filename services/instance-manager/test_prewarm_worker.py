#!/usr/bin/env python3
"""阶段 4：预热 Worker 容量决策的纯本地测试。"""

import tempfile
import unittest
import hashlib
from pathlib import Path

from prewarm_config import PrewarmConfig
from prewarm_capacity import HostCapacity
from prewarm_claim_audit import StaleClaimAudit
from prewarm_claim_compensation import ClaimCompensationResult
from prewarm_store import PrewarmStore
from prewarm_worker import PrewarmWorker, PrewarmWorkerError


class PrewarmWorkerTests(unittest.TestCase):
    class FakeClaimAuditor:
        def __init__(self, audits) -> None:
            self.audits = tuple(audits)
            self.timeouts: list[int] = []

        def audit(self, timeout_seconds: int):
            self.timeouts.append(timeout_seconds)
            return self.audits

    class FakeClaimCompensator:
        def __init__(self, failures=()) -> None:
            self.failures = set(failures)
            self.calls: list[str] = []

        def compensate(self, audit: StaleClaimAudit) -> ClaimCompensationResult:
            self.calls.append(audit.runtime_id)
            if audit.runtime_id in self.failures:
                raise RuntimeError("generation mismatch")
            return ClaimCompensationResult(
                runtime_id=audit.runtime_id,
                operation_id=audit.operation_id,
                status="retired",
            )

    class FakeCapacityProbe:
        def __init__(self, available_mib: int, free_network_slots: int) -> None:
            self.capacity = HostCapacity(available_mib, free_network_slots)
            self.calls = 0

        def snapshot(self) -> HostCapacity:
            self.calls += 1
            return self.capacity

    class FakeBuilder:
        def __init__(self) -> None:
            self.fail_prepare = False
            self.fail_on_call: int | None = None
            self.fail_stop = False
            self.fail_destroy = False
            self.prepared: list[tuple[str, str, int]] = []
            self.stopped: list[tuple[str, str]] = []
            self.destroyed: list[tuple[str, bool]] = []
            self.statuses: dict[str, dict | None] = {}

        def prepare_prewarmer(
            self,
            runtime_id: str,
            claim_token: str,
            *,
            min_free_network_slots: int,
        ) -> dict:
            self.prepared.append(
                (runtime_id, claim_token, min_free_network_slots)
            )
            if self.fail_prepare or self.fail_on_call == len(self.prepared):
                raise RuntimeError("prepare failed")
            return {"pid": 1234, "processStartTicks": 5678}

        def destroy(self, instance_id: str, *, preserve_volume: bool = True) -> None:
            if self.fail_destroy:
                raise RuntimeError("destroy failed")
            self.destroyed.append((instance_id, preserve_volume))

        def stop(self, instance_id: str, reason: str = "api") -> dict:
            self.stopped.append((instance_id, reason))
            if self.fail_stop:
                raise RuntimeError("stop failed")
            existing = self.statuses.get(instance_id) or {"id": instance_id}
            self.statuses[instance_id] = {
                **existing,
                "status": "stopped",
                "processAlive": False,
            }
            return {"id": instance_id, "status": "stopped"}

        def status(self, instance_id: str) -> dict | None:
            return self.statuses.get(instance_id)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = PrewarmStore(Path(self.temporary.name) / "control-plane.db")
        self.builder = self.FakeBuilder()
        self.runtime_index = 0

    def next_runtime_id(self) -> str:
        self.runtime_index += 1
        return f"prewarm-test-{self.runtime_index}"

    def worker(
        self,
        *,
        with_builder: bool = False,
        capacity_probe=None,
        claim_auditor=None,
        claim_compensator=None,
        **overrides: str,
    ) -> PrewarmWorker:
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
            builder=self.builder if with_builder else None,
            capacity_probe=capacity_probe,
            host_id="host-test",
            new_runtime_id=self.next_runtime_id,
            new_claim_token=lambda: "raw-claim-token",
            claim_auditor=claim_auditor,
            claim_compensator=claim_compensator,
        )

    @staticmethod
    def claim_audit(runtime_id: str, classification: str) -> StaleClaimAudit:
        return StaleClaimAudit(
            runtime_id=runtime_id,
            logical_instance_id=f"logical-{runtime_id}",
            operation_id=f"operation-{runtime_id}",
            classification=classification,
            operation_status="executing",
            attachment_status=None,
            binding_status=None,
        )

    def test_stale_claim_recovery_compensates_pre_activation_timeouts(self) -> None:
        auditor = self.FakeClaimAuditor(
            (
                self.claim_audit("safe-1", "pre_attach_timeout"),
                self.claim_audit("reserved-1", "reserved_timeout"),
                self.claim_audit("activating-1", "activation_timeout"),
            )
        )
        compensator = self.FakeClaimCompensator()
        result = self.worker(
            claim_auditor=auditor,
            claim_compensator=compensator,
            PREWARM_CLAIM_TIMEOUT_SECONDS="45",
        ).recover_stale_claims()
        self.assertEqual(auditor.timeouts, [45])
        self.assertEqual(compensator.calls, ["safe-1", "reserved-1"])
        self.assertEqual(result.compensated_runtime_ids, ("safe-1", "reserved-1"))
        self.assertEqual(
            result.deferred_classifications,
            ("activating-1:activation_timeout",),
        )
        self.assertEqual(result.errors, ())

    def test_stale_claim_compensation_error_does_not_stop_remaining_items(self) -> None:
        auditor = self.FakeClaimAuditor(
            (
                self.claim_audit("failed-1", "pre_attach_timeout"),
                self.claim_audit("safe-2", "pre_attach_timeout"),
            )
        )
        compensator = self.FakeClaimCompensator(failures=("failed-1",))
        result = self.worker(
            claim_auditor=auditor,
            claim_compensator=compensator,
        ).recover_stale_claims()
        self.assertEqual(compensator.calls, ["failed-1", "safe-2"])
        self.assertEqual(result.compensated_runtime_ids, ("safe-2",))
        self.assertEqual(len(result.errors), 1)
        self.assertIn("failed-1:pre_attach_timeout:generation mismatch", result.errors[0])

    def preparing(self, runtime_id: str) -> None:
        self.store.create_item(runtime_id, "profile-a")

    def test_update_config_replaces_only_changed_valid_config(self) -> None:
        worker = self.worker(with_builder=True)
        original = worker.config
        self.assertFalse(worker.update_config(original))
        disabled = PrewarmConfig.from_env({"PREWARM_ENABLED": "0"})
        self.assertTrue(worker.update_config(disabled))
        self.assertIs(worker.config, disabled)

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

    def test_prepare_one_publishes_ready_item_with_runtime_generation(self) -> None:
        item = self.worker(with_builder=True).prepare_one()
        self.assertEqual(item["runtime_id"], "prewarm-test-1")
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["host_id"], "host-test")
        self.assertEqual(item["vmm_pid"], 1234)
        self.assertEqual(item["process_start_ticks"], 5678)
        self.assertEqual(
            item["claim_token_hash"],
            hashlib.sha256(b"raw-claim-token").hexdigest(),
        )
        self.assertNotEqual(item["claim_token_hash"], "raw-claim-token")
        self.assertEqual(
            self.builder.prepared,
            [("prewarm-test-1", "raw-claim-token", 2)],
        )
        self.assertEqual(self.builder.destroyed, [])

    def test_prepare_failure_records_error_without_publishing_ready(self) -> None:
        self.builder.fail_prepare = True
        with self.assertRaisesRegex(PrewarmWorkerError, "prepare failed"):
            self.worker(with_builder=True).prepare_one()
        item = self.store.get("prewarm-test-1")
        self.assertEqual(item["status"], "error")
        self.assertEqual(item["last_error"], "prepare failed")
        self.assertEqual(self.builder.destroyed, [])

    def test_prepare_one_rejects_disabled_worker(self) -> None:
        with self.assertRaisesRegex(PrewarmWorkerError, "disabled"):
            self.worker(with_builder=True, PREWARM_ENABLED="0").prepare_one()
        self.assertIsNone(self.store.get("prewarm-test-1"))

    def test_prepare_one_requires_builder(self) -> None:
        with self.assertRaisesRegex(PrewarmWorkerError, "not configured"):
            self.worker().prepare_one()

    def test_fill_once_creates_planned_number_of_items(self) -> None:
        result = self.worker(with_builder=True).fill_once(
            available_mib=4096,
            free_network_slots=10,
        )
        self.assertEqual(result.plan.create_count, 2)
        self.assertEqual(
            result.created_runtime_ids,
            ("prewarm-test-1", "prewarm-test-2"),
        )
        self.assertEqual(self.store.get("prewarm-test-1")["status"], "ready")
        self.assertEqual(self.store.get("prewarm-test-2")["status"], "ready")

    def test_fill_once_only_creates_current_deficit(self) -> None:
        self.ready("existing-ready")
        result = self.worker(with_builder=True).fill_once(
            available_mib=4096,
            free_network_slots=10,
        )
        self.assertEqual(result.plan.create_count, 1)
        self.assertEqual(result.created_runtime_ids, ("prewarm-test-1",))

    def test_fill_once_does_nothing_when_disabled(self) -> None:
        result = self.worker(
            with_builder=True,
            PREWARM_ENABLED="0",
        ).fill_once(available_mib=4096, free_network_slots=10)
        self.assertEqual(result.plan.reason, "disabled")
        self.assertEqual(result.created_runtime_ids, ())
        self.assertEqual(self.builder.prepared, [])

    def test_fill_once_stops_after_first_prepare_failure(self) -> None:
        self.builder.fail_on_call = 2
        with self.assertRaisesRegex(PrewarmWorkerError, "prepare failed"):
            self.worker(with_builder=True).fill_once(
                available_mib=4096,
                free_network_slots=10,
            )
        self.assertEqual(len(self.builder.prepared), 2)
        self.assertEqual(self.store.get("prewarm-test-1")["status"], "ready")
        self.assertEqual(self.store.get("prewarm-test-2")["status"], "error")

    def test_fill_once_from_host_uses_capacity_probe(self) -> None:
        probe = self.FakeCapacityProbe(4096, 3)
        result = self.worker(
            with_builder=True,
            capacity_probe=probe,
        ).fill_once_from_host()
        self.assertEqual(probe.calls, 1)
        self.assertEqual(result.plan.create_count, 1)
        self.assertEqual(result.created_runtime_ids, ("prewarm-test-1",))

    def test_fill_once_from_host_requires_probe(self) -> None:
        with self.assertRaisesRegex(PrewarmWorkerError, "probe is not configured"):
            self.worker(with_builder=True).fill_once_from_host()

    def test_disabled_fill_skips_capacity_probe(self) -> None:
        result = self.worker(
            with_builder=True,
            PREWARM_ENABLED="0",
        ).fill_once_from_host()
        self.assertEqual(result.plan.reason, "disabled")
        self.assertEqual(result.created_runtime_ids, ())

    def test_reap_expired_ready_stops_destroys_and_retires(self) -> None:
        self.ready("expired-1", ttl_seconds=-1)
        self.ready("valid-1", ttl_seconds=900)
        retired = self.worker(with_builder=True).reap_expired_ready()
        self.assertEqual(retired, ("expired-1",))
        self.assertEqual(
            self.builder.stopped,
            [("expired-1", "prewarm_ttl_expired")],
        )
        self.assertEqual(self.builder.destroyed, [("expired-1", True)])
        self.assertEqual(self.store.get("expired-1")["status"], "retired")
        self.assertEqual(self.store.get("valid-1")["status"], "ready")

    def test_reap_skips_item_lost_to_concurrent_transition(self) -> None:
        self.ready("expired-1", ttl_seconds=-1)
        original = self.store.mark_draining
        self.store.mark_draining = lambda _runtime_id: False  # type: ignore[method-assign]
        try:
            retired = self.worker(with_builder=True).reap_expired_ready()
        finally:
            self.store.mark_draining = original  # type: ignore[method-assign]
        self.assertEqual(retired, ())
        self.assertEqual(self.builder.stopped, [])
        self.assertEqual(self.store.get("expired-1")["status"], "ready")

    def test_reap_stop_failure_leaves_item_draining(self) -> None:
        self.ready("expired-1", ttl_seconds=-1)
        self.builder.fail_stop = True
        with self.assertRaisesRegex(PrewarmWorkerError, "stop failed"):
            self.worker(with_builder=True).reap_expired_ready()
        self.assertEqual(self.store.get("expired-1")["status"], "draining")
        self.assertEqual(self.builder.destroyed, [])

    def test_reap_destroy_failure_leaves_item_draining(self) -> None:
        self.ready("expired-1", ttl_seconds=-1)
        self.builder.fail_destroy = True
        with self.assertRaisesRegex(PrewarmWorkerError, "destroy failed"):
            self.worker(with_builder=True).reap_expired_ready()
        self.assertEqual(self.store.get("expired-1")["status"], "draining")

    def runtime_status(
        self,
        runtime_id: str,
        *,
        pid: int = 1234,
        start_ticks: int = 5678,
        alive: bool = True,
    ) -> None:
        self.builder.statuses[runtime_id] = {
            "id": runtime_id,
            "status": "running" if alive else "stopped",
            "processAlive": alive,
            "pid": pid,
            "processStartTicks": start_ticks,
        }

    def test_reconcile_keeps_healthy_ready_generation(self) -> None:
        self.preparing("ready-1")
        self.assertTrue(self.store.record_runtime_generation("ready-1", 1234, 5678))
        self.assertTrue(self.store.mark_ready("ready-1", ttl_seconds=900))
        self.runtime_status("ready-1")
        result = self.worker(with_builder=True).reconcile_public()
        self.assertEqual(result.kept_runtime_ids, ("ready-1",))
        self.assertEqual(result.retired_runtime_ids, ())
        self.assertEqual(self.builder.stopped, [])

    def test_reconcile_retires_dead_ready_runtime(self) -> None:
        self.preparing("ready-dead")
        self.assertTrue(self.store.record_runtime_generation("ready-dead", 1234, 5678))
        self.assertTrue(self.store.mark_ready("ready-dead", ttl_seconds=900))
        self.runtime_status("ready-dead", alive=False)
        result = self.worker(with_builder=True).reconcile_public()
        self.assertEqual(result.retired_runtime_ids, ("ready-dead",))
        self.assertEqual(self.store.get("ready-dead")["status"], "retired")
        self.assertEqual(self.builder.stopped, [])
        self.assertEqual(self.builder.destroyed, [("ready-dead", True)])

    def test_reconcile_cleans_interrupted_preparing_runtime(self) -> None:
        self.preparing("building-1")
        self.assertTrue(self.store.record_runtime_generation("building-1", 1234, 5678))
        self.runtime_status("building-1")
        result = self.worker(with_builder=True).reconcile_public()
        self.assertEqual(result.retired_runtime_ids, ("building-1",))
        self.assertEqual(
            self.builder.stopped,
            [("building-1", "prewarm_reconcile")],
        )
        self.assertEqual(self.store.get("building-1")["status"], "retired")

    def test_reconcile_retires_existing_draining_item(self) -> None:
        self.ready("draining-1")
        self.assertTrue(self.store.mark_draining("draining-1"))
        result = self.worker(with_builder=True).reconcile_public()
        self.assertEqual(result.retired_runtime_ids, ("draining-1",))
        self.assertEqual(self.store.get("draining-1")["status"], "retired")

    def test_reconcile_quarantines_generation_mismatch_without_stopping(self) -> None:
        self.preparing("ready-mismatch")
        self.assertTrue(
            self.store.record_runtime_generation("ready-mismatch", 1234, 5678)
        )
        self.assertTrue(self.store.mark_ready("ready-mismatch", ttl_seconds=900))
        self.runtime_status("ready-mismatch", pid=9999, start_ticks=7777)
        with self.assertRaisesRegex(PrewarmWorkerError, "refusing to stop mismatched"):
            self.worker(with_builder=True).reconcile_public()
        self.assertEqual(self.store.get("ready-mismatch")["status"], "draining")
        self.assertEqual(self.builder.stopped, [])
        self.assertEqual(self.builder.destroyed, [])

    def test_health_check_keeps_matching_ready_runtime(self) -> None:
        self.preparing("ready-healthy")
        self.assertTrue(
            self.store.record_runtime_generation("ready-healthy", 1234, 5678)
        )
        self.assertTrue(self.store.mark_ready("ready-healthy", ttl_seconds=900))
        self.runtime_status("ready-healthy")
        result = self.worker(with_builder=True).check_ready_health()
        self.assertEqual(result.healthy_runtime_ids, ("ready-healthy",))
        self.assertEqual(result.retired_runtime_ids, ())

    def test_health_check_retires_dead_ready_runtime(self) -> None:
        self.preparing("ready-dead")
        self.assertTrue(self.store.record_runtime_generation("ready-dead", 1234, 5678))
        self.assertTrue(self.store.mark_ready("ready-dead", ttl_seconds=900))
        self.runtime_status("ready-dead", alive=False)
        result = self.worker(with_builder=True).check_ready_health()
        self.assertEqual(result.retired_runtime_ids, ("ready-dead",))
        self.assertEqual(self.store.get("ready-dead")["status"], "retired")
        self.assertEqual(self.builder.destroyed, [("ready-dead", True)])

    def test_health_check_quarantines_generation_mismatch(self) -> None:
        self.preparing("ready-mismatch")
        self.assertTrue(
            self.store.record_runtime_generation("ready-mismatch", 1234, 5678)
        )
        self.assertTrue(self.store.mark_ready("ready-mismatch", ttl_seconds=900))
        self.runtime_status("ready-mismatch", pid=9999, start_ticks=7777)
        with self.assertRaisesRegex(PrewarmWorkerError, "refusing to stop mismatched"):
            self.worker(with_builder=True).check_ready_health()
        self.assertEqual(self.store.get("ready-mismatch")["status"], "draining")
        self.assertEqual(self.builder.destroyed, [])

    def ready_with_generation(self, runtime_id: str) -> None:
        self.preparing(runtime_id)
        self.assertTrue(self.store.record_runtime_generation(runtime_id, 1234, 5678))
        self.assertTrue(self.store.mark_ready(runtime_id, ttl_seconds=900))
        self.runtime_status(runtime_id)

    def test_disabled_worker_drains_all_public_ready_items(self) -> None:
        self.ready_with_generation("ready-1")
        self.ready_with_generation("ready-2")
        retired = self.worker(
            with_builder=True,
            PREWARM_ENABLED="0",
        ).drain_excess_ready()
        self.assertEqual(retired, ("ready-1", "ready-2"))
        self.assertEqual(self.store.get("ready-1")["status"], "retired")
        self.assertEqual(self.store.get("ready-2")["status"], "retired")

    def test_downscale_drains_only_excess_ready_items(self) -> None:
        self.ready_with_generation("ready-1")
        self.ready_with_generation("ready-2")
        self.ready_with_generation("ready-3")
        retired = self.worker(
            with_builder=True,
            PREWARM_TARGET_READY="1",
            PREWARM_MAX_TOTAL="3",
        ).drain_excess_ready()
        self.assertEqual(retired, ("ready-1", "ready-2"))
        self.assertEqual(self.store.get("ready-3")["status"], "ready")

    def test_resize_does_not_touch_claiming_item(self) -> None:
        self.ready_with_generation("claimed-1")
        self.assertIsNotNone(
            self.store.claim(
                "profile-a",
                "logical-1",
                "user-1",
                "/vol/1.ext4",
                "operation-1",
                "activation-1",
            )
        )
        retired = self.worker(
            with_builder=True,
            PREWARM_ENABLED="0",
        ).drain_excess_ready()
        self.assertEqual(retired, ())
        self.assertEqual(self.store.get("claimed-1")["status"], "claiming")
        self.assertEqual(self.builder.stopped, [])

    def test_resize_stop_failure_leaves_item_draining(self) -> None:
        self.ready_with_generation("ready-1")
        self.builder.fail_stop = True
        with self.assertRaisesRegex(PrewarmWorkerError, "stop failed"):
            self.worker(
                with_builder=True,
                PREWARM_ENABLED="0",
            ).drain_excess_ready()
        self.assertEqual(self.store.get("ready-1")["status"], "draining")


if __name__ == "__main__":
    unittest.main()
