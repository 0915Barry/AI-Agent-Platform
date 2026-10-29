#!/usr/bin/env python3
"""WorkspaceStore 的本地 SQLite 单元测试。"""

import tempfile
import unittest
from pathlib import Path

from workspace_store import WorkspaceStore, WorkspaceStoreError


class WorkspaceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = WorkspaceStore(Path(self.temporary.name) / "control-plane.db")

    def test_create_select_and_list_user_workspaces(self) -> None:
        first = self.store.ensure_default("user-one")
        second = self.store.create("user-one", "AI Agent Platform")
        self.assertFalse(self.store.get("user-one", first["id"])["current"])
        self.assertTrue(second["current"])
        self.assertEqual(self.store.current("user-one")["id"], second["id"])
        self.assertEqual(len(self.store.list("user-one")), 2)

        selected = self.store.select("user-one", first["id"])
        self.assertTrue(selected["current"])

    def test_names_are_unique_per_user_but_not_globally(self) -> None:
        self.store.create("user-one", "Project A")
        with self.assertRaises(WorkspaceStoreError):
            self.store.create("user-one", "project a")
        self.store.create("user-two", "Project A")

    def test_user_cannot_select_another_users_workspace(self) -> None:
        workspace = self.store.create("user-one", "Private")
        with self.assertRaises(WorkspaceStoreError):
            self.store.select("user-two", workspace["id"])

    def test_delete_removes_workspace_and_current_preference(self) -> None:
        workspace = self.store.create("user-one", "Temporary")
        deleted = self.store.delete("user-one", workspace["id"])
        self.assertEqual(deleted["id"], workspace["id"])
        self.assertIsNone(self.store.current("user-one"))
        with self.assertRaises(WorkspaceStoreError):
            self.store.get("user-one", workspace["id"])


if __name__ == "__main__":
    unittest.main()
