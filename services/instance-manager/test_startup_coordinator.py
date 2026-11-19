#!/usr/bin/env python3
"""StartupCoordinator 的 fake Runtime 单元测试。"""

import tempfile
import unittest
from pathlib import Path

from prewarm_config import PrewarmConfig
from prewarm_manager import ClaimResult
from startup_coordinator import StartupCoordinator, StartupResult
from startup_store import StartupStore, StartupStoreError


class FakeRuntime:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.started: list[tuple[str, str | None]] = []
        self.resumed: list[tuple[str, str | None]] = []
        self.phases: dict = {}
        self.guest_ready_ms: int | None = None

    def start(self, instance_id: str, owner_id: str | None) -> dict:
        self.started.append((instance_id, owner_id))
        if self.fail:
            raise RuntimeError("cold start failed")
        result = {
            "status": "running",
            "instanceId": instance_id,
            "startupOperationPhases": dict(self.phases),
        }
        if self.guest_ready_ms is not None:
            result["startupOperationMilestones"] = {
                "guestDeclaredRpcReadyMs": self.guest_ready_ms
            }
        return result

    def resume(self, instance_id: str, owner_id: str | None) -> dict:
        self.resumed.append((instance_id, owner_id))
        if self.fail:
            raise RuntimeError("resume failed")
        return {"status": "running", "instanceId": instance_id}


class FakePrewarmManager:
    def __init__(self, *, enabled: bool = True) -> None:
        self.config = PrewarmConfig.from_env({"PREWARM_ENABLED": "1" if enabled else "0"})
        self.result = ClaimResult(claimed=False, reason="unavailable")
        self.error: Exception | None = None
        self.claimed: list[tuple[str, str, str, str, str]] = []
        self.committed: list[tuple[str, str, str, str]] = []

    def claim(
        self,
        profile_key: str,
        instance_id: str,
        owner_id: str,
        volume_path: str,
        operation_id: str,
    ) -> ClaimResult:
        self.claimed.append(
            (profile_key, instance_id, owner_id, volume_path, operation_id)
        )
        if self.error is not None:
            raise self.error
        return self.result

    def commit(
        self,
        instance_id: str,
        runtime_id: str,
        operation_id: str,
        activation_id: str,
    ) -> None:
        self.committed.append(
            (instance_id, runtime_id, operation_id, activation_id)
        )

class StartupCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "control-plane.db"
        self.store = StartupStore(self.database)
        self.runtime = FakeRuntime()
        counter = 0

        def make_operation_id() -> str:
            nonlocal counter
            counter += 1
            return f"op-{counter}"

        self.coordinator = StartupCoordinator(
            self.store, self.runtime, new_operation_id=make_operation_id
        )

    def test_run_cold_records_and_executes(self) -> None:
        result = self.coordinator.run_cold("inst-1", "user-1")
        self.assertIsInstance(result, StartupResult)
        self.assertEqual(result.strategy, "cold")
        self.assertEqual(self.runtime.started, [("inst-1", "user-1")])
        self.assertEqual(self.store.get(result.operation_id)["status"], "committing")

    def test_run_cold_carries_runtime_phase_report(self) -> None:
        self.runtime.phases = {"volumePrepareMs": 80, "runtimeReadyMs": 4800}
        result = self.coordinator.run_cold("inst-1", "user-1")
        self.assertEqual(result.phase_report["volumePrepareMs"], 80)
        self.assertEqual(result.phase_report["runtimeReadyMs"], 4800)
        self.assertGreaterEqual(result.phase_report["decisionMs"], 0)

    def test_run_cold_offsets_runtime_guest_ready_milestone(self) -> None:
        self.runtime.guest_ready_ms = 4800
        result = self.coordinator.run_cold("inst-1", "user-1")
        self.assertIsNotNone(result.milestone_report)
        self.assertGreaterEqual(
            result.milestone_report["guestDeclaredRpcReadyMs"], 4800
        )

    def test_run_snapshot_records_and_executes(self) -> None:
        result = self.coordinator.run_snapshot("inst-1", "user-1")
        self.assertEqual(result.strategy, "snapshot")
        self.assertEqual(self.runtime.resumed, [("inst-1", "user-1")])
        self.assertEqual(self.store.get(result.operation_id)["status"], "committing")

    def test_commit_success(self) -> None:
        result = self.coordinator.run_cold("inst-1", "user-1")
        self.coordinator.commit_success(result.operation_id)
        self.assertEqual(self.store.get("op-1")["status"], "succeeded")

    def test_failure_marks_failed_and_raises(self) -> None:
        self.runtime.fail = True
        with self.assertRaises(RuntimeError):
            self.coordinator.run_cold("inst-1", "user-1")
        self.assertEqual(self.store.get("op-1")["status"], "failed")
        self.assertEqual(self.store.get("op-1")["last_error"], "cold start failed")

    def test_second_active_operation_rejected(self) -> None:
        self.coordinator.run_cold("inst-1", "user-1")
        # 第一个操作还停在 committing，同一逻辑实例不能开始第二个操作。
        with self.assertRaises(StartupStoreError):
            self.coordinator.run_cold("inst-1", "user-1")

    def test_success_then_new_operation_allowed(self) -> None:
        result = self.coordinator.run_cold("inst-1", "user-1")
        self.coordinator.commit_success(result.operation_id)
        # succeeded 是终态，允许同一实例再次启动。
        self.coordinator.run_cold("inst-1", "user-1")

    def test_commit_failure(self) -> None:
        result = self.coordinator.run_cold("inst-1", "user-1")
        self.coordinator.commit_failure(result.operation_id, "commit error")
        self.assertEqual(self.store.get("op-1")["status"], "failed")
        self.assertEqual(self.store.get("op-1")["last_error"], "commit error")

    def test_run_start_uses_prewarm_and_commits_guest(self) -> None:
        prewarm = FakePrewarmManager()
        prewarm.result = ClaimResult(
            claimed=True,
            runtime_id="runtime-1",
            activation_id="activation-1",
            operation_id="op-1",
            runtime={"status": "running", "id": "runtime-1"},
        )
        coordinator = StartupCoordinator(
            self.store,
            self.runtime,
            prewarm_manager=prewarm,  # type: ignore[arg-type]
            new_operation_id=lambda: "op-1",
        )

        result = coordinator.run_start(
            "inst-1",
            "user-1",
            volume_path="/srv/fc/volumes/users/user-1.ext4",
        )

        self.assertEqual(result.strategy, "prewarm")
        self.assertEqual(self.runtime.started, [])
        self.assertEqual(self.store.get("op-1")["status"], "committing")
        coordinator.commit_success(result)
        self.assertEqual(self.store.get("op-1")["status"], "succeeded")
        self.assertEqual(
            prewarm.committed,
            [("inst-1", "runtime-1", "op-1", "activation-1")],
        )

    def test_run_start_prewarm_carries_claim_phase_report(self) -> None:
        prewarm = FakePrewarmManager()
        prewarm.result = ClaimResult(
            claimed=True,
            runtime_id="runtime-1",
            activation_id="activation-1",
            operation_id="op-1",
            runtime={"status": "running", "id": "runtime-1"},
            phase_report={"claimMs": 5, "guestActivateMs": 300},
            milestone_report={"guestDeclaredRpcReadyMs": 385},
        )
        coordinator = StartupCoordinator(
            self.store,
            self.runtime,
            prewarm_manager=prewarm,  # type: ignore[arg-type]
            new_operation_id=lambda: "op-1",
        )
        result = coordinator.run_start(
            "inst-1",
            "user-1",
            volume_path="/srv/fc/volumes/users/user-1.ext4",
        )
        self.assertEqual(result.strategy, "prewarm")
        self.assertEqual(result.phase_report["claimMs"], 5)
        self.assertEqual(result.phase_report["guestActivateMs"], 300)
        self.assertGreaterEqual(result.phase_report["decisionMs"], 0)
        self.assertGreaterEqual(
            result.milestone_report["guestDeclaredRpcReadyMs"], 385
        )

    def test_run_start_falls_back_only_when_pool_is_unavailable(self) -> None:
        prewarm = FakePrewarmManager()
        operation_ids = iter(("prewarm-op", "cold-op"))
        coordinator = StartupCoordinator(
            self.store,
            self.runtime,
            prewarm_manager=prewarm,  # type: ignore[arg-type]
            new_operation_id=lambda: next(operation_ids),
        )

        result = coordinator.run_start(
            "inst-1",
            "user-1",
            volume_path="/srv/fc/volumes/users/user-1.ext4",
        )

        self.assertEqual(result.strategy, "cold")
        self.assertEqual(self.runtime.started, [("inst-1", "user-1")])
        self.assertEqual(self.store.get("prewarm-op")["status"], "failed")
        self.assertEqual(self.store.get("cold-op")["status"], "committing")

    def test_run_start_does_not_fallback_after_activation_error(self) -> None:
        prewarm = FakePrewarmManager()
        prewarm.error = RuntimeError("activation uncertain")
        coordinator = StartupCoordinator(
            self.store,
            self.runtime,
            prewarm_manager=prewarm,  # type: ignore[arg-type]
            new_operation_id=lambda: "prewarm-op",
        )

        with self.assertRaisesRegex(RuntimeError, "activation uncertain"):
            coordinator.run_start(
                "inst-1",
                "user-1",
                volume_path="/srv/fc/volumes/users/user-1.ext4",
            )

        self.assertEqual(self.runtime.started, [])
        self.assertEqual(self.store.get("prewarm-op")["status"], "failed")

    def test_run_start_skips_prewarm_when_disabled(self) -> None:
        prewarm = FakePrewarmManager(enabled=False)
        coordinator = StartupCoordinator(
            self.store,
            self.runtime,
            prewarm_manager=prewarm,  # type: ignore[arg-type]
            new_operation_id=lambda: "cold-op",
        )

        result = coordinator.run_start(
            "inst-1",
            "user-1",
            volume_path="/srv/fc/volumes/users/user-1.ext4",
        )

        self.assertEqual(result.strategy, "cold")
        self.assertEqual(prewarm.claimed, [])


if __name__ == "__main__":
    unittest.main()
