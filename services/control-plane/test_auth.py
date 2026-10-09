#!/usr/bin/env python3
"""M16 密码、会话和旧实例归属迁移测试。"""

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from auth import AuthError, AuthStore


class AuthStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "control-plane.db"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_password_is_hashed_and_session_can_be_revoked(self) -> None:
        store = AuthStore(self.database)
        user = store.create_user("alice", "correct horse battery")
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT password_salt,password_hash FROM users WHERE id=?", (user["id"],)
            ).fetchone()
        assert row is not None
        self.assertNotIn(b"correct horse battery", row[0] + row[1])
        self.assertIsNone(store.authenticate("alice", "wrong password value"))
        self.assertEqual(store.authenticate("alice", "correct horse battery"), user)

        token, expires_at = store.create_session(user["id"])
        self.assertGreater(expires_at, int(time.time()))
        self.assertEqual(store.session_user(token), user)
        store.revoke_session(token)
        self.assertIsNone(store.session_user(token))

    def test_first_user_claims_legacy_instances(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE instances (id TEXT PRIMARY KEY,status TEXT NOT NULL,created_at INTEGER "
                "NOT NULL,updated_at INTEGER NOT NULL,last_error TEXT)"
            )
            connection.execute("INSERT INTO instances VALUES('legacy','stopped',1,1,NULL)")
        store = AuthStore(self.database)
        user = store.create_user("owner", "a sufficiently long password", claim_unowned=True)
        with sqlite3.connect(self.database) as connection:
            owner = connection.execute(
                "SELECT owner_id FROM instances WHERE id='legacy'"
            ).fetchone()[0]
        self.assertEqual(owner, user["id"])

    def test_rejects_weak_password_and_invalid_username(self) -> None:
        store = AuthStore(self.database)
        with self.assertRaises(AuthError):
            store.create_user("UP", "a sufficiently long password")
        with self.assertRaises(AuthError):
            store.create_user("valid-user", "short")


if __name__ == "__main__":
    unittest.main()
