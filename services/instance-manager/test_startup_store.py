#!/usr/bin/env python3
"""StartupStore 启动操作代次的本地单元测试（阶段 1）。"""

import tempfile
import unittest
from pathlib import Path

from startup_store import StartupStore, StartupStoreError


class StartupStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "control-plane.db"
        self.store = StartupStore(self.database)

    def test_begin_and_get(self) -> None:
        op = self.store.begin("op-1", "logical-1", "user-1", "prewarm")
        self.assertEqual(op["status"], "deciding")
        self.assertEqual(op["strategy"], "prewarm")
        self.assertEqual(self.store.get("op-1")["logical_instance_id"], "logical-1")

    def test_begin_rejects_second_active_operation(self) -> None:
        self.store.begin("op-1", "logical-1", "user-1", "cold")
        with self.assertRaises(StartupStoreError):
            self.store.begin("op-2", "logical-1", "user-1", "prewarm")

    def test_get_active_returns_active_operation(self) -> None:
        self.store.begin("op-1", "logical-1", "user-1", "cold")
        self.assertEqual(self.store.get_active("logical-1")["operation_id"], "op-1")

    def test_succeeded_is_terminal_and_allows_new_operation(self) -> None:
        self.store.begin("op-1", "logical-1", "user-1", "cold")
        self.store.transition("op-1", "deciding", "executing", runtime_id="rt-1")
        self.store.transition("op-1", "executing", "committing")
        self.assertTrue(self.store.mark_succeeded("op-1"))
        self.assertEqual(self.store.get("op-1")["status"], "succeeded")
        # 终态不再占用唯一索引，允许同一逻辑实例发起新操作。
        self.store.begin("op-2", "logical-1", "user-1", "cold")

    def test_transition_rejects_stale_status(self) -> None:
        self.store.begin("op-1", "logical-1", "user-1", "cold")
        # 期望 status 不匹配，转换失败。
        self.assertFalse(self.store.transition("op-1", "executing", "committing"))
        self.assertEqual(self.store.get("op-1")["status"], "deciding")

    def test_transition_rejects_unknown_operation(self) -> None:
        self.assertFalse(self.store.transition("op-missing", "deciding", "executing"))

    def test_mark_failed_records_error(self) -> None:
        self.store.begin("op-1", "logical-1", "user-1", "cold")
        self.assertTrue(self.store.mark_failed("op-1", "claim failed"))
        op = self.store.get("op-1")
        self.assertEqual(op["status"], "failed")
        self.assertEqual(op["last_error"], "claim failed")
        # failed 也是终态，允许新操作。
        self.store.begin("op-2", "logical-1", "user-1", "cold")


if __name__ == "__main__":
    unittest.main()
