#!/usr/bin/env python3
"""StartupCoordinator 的单元测试（阶段 2，用 fake runtime）。"""

import tempfile
import unittest
from pathlib import Path

from startup_coordinator import StartupCoordinator, StartupResult
from startup_store import StartupStore, StartupStoreError


class FakeRuntime:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.started: list[tuple[str, str | None]] = []
        self.resumed: list[tuple[str, str | None]] = []

    def start(self, instance_id: str, owner_id: str | None) -> dict:
        self.started.append((instance_id, owner_id))
        if self.fail:
            raise RuntimeError("cold start failed")
        return {"status": "running", "instanceId": instance_id}

    def resume(self, instance_id: str, owner_id: str | None) -> dict:
        self.resumed.append((instance_id, owner_id))
        if self.fail:
            raise RuntimeError("resume failed")
        return {"status": "running", "instanceId": instance_id}


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


if __name__ == "__main__":
    unittest.main()
