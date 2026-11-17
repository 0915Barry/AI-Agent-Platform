#!/usr/bin/env python3
"""超时预热领取只读分类测试。"""

import tempfile
import unittest
from pathlib import Path

from prewarm_claim_audit import PrewarmClaimAuditor
from prewarm_store import PrewarmStore
from runtime_store import RuntimeStore, VolumeAttachmentStore
from startup_store import StartupStore


class PrewarmClaimAuditorTests(unittest.TestCase):
    NOW = 10_000

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "control-plane.db"
        self.prewarm = PrewarmStore(self.database)
        self.startups = StartupStore(self.database)
        self.attachments = VolumeAttachmentStore(self.database)
        self.runtimes = RuntimeStore(self.database)
        self.auditor = PrewarmClaimAuditor(
            prewarm_store=self.prewarm,
            startup_store=self.startups,
            attachment_store=self.attachments,
            runtime_store=self.runtimes,
            profile_key="profile-a",
        )

    def stale_claim(self, *, with_operation: bool = True) -> None:
        self.prewarm.create_item("runtime-1", "profile-a")
        self.assertTrue(self.prewarm.mark_ready("runtime-1", ttl_seconds=900))
        if with_operation:
            self.startups.begin("operation-1", "logical-1", "user-1", "prewarm")
            self.assertTrue(
                self.startups.transition("operation-1", "deciding", "executing")
            )
        self.assertIsNotNone(
            self.prewarm.claim(
                "profile-a",
                "logical-1",
                "user-1",
                "/vol/user-1.ext4",
                "operation-1",
                "activation-1",
            )
        )
        with self.prewarm.connect() as connection:
            connection.execute(
                "UPDATE prewarm_items SET claimed_at=? WHERE runtime_id='runtime-1'",
                (self.NOW - 100,),
            )

    def classification(self) -> str:
        results = self.auditor.audit(30, now=self.NOW)
        self.assertEqual(len(results), 1)
        return results[0].classification

    def reserve_attachment(self) -> None:
        self.attachments.reserve(
            "/vol/user-1.ext4",
            "user-1",
            "logical-1",
            "runtime-1",
            "operation-1",
        )

    def save_binding(self, status: str) -> None:
        self.runtimes.save_binding(
            {
                "logicalInstanceId": "logical-1",
                "runtimeId": "runtime-1",
                "userId": "user-1",
                "volumePath": "/vol/user-1.ext4",
                "activationId": "activation-1",
                "status": status,
            }
        )

    def test_recent_claim_is_not_reported(self) -> None:
        self.stale_claim()
        self.assertEqual(self.auditor.audit(200, now=self.NOW), ())

    def test_missing_operation_is_classified(self) -> None:
        self.stale_claim(with_operation=False)
        self.assertEqual(self.classification(), "operation_missing")

    def test_pre_attach_timeout_is_classified(self) -> None:
        self.stale_claim()
        self.assertEqual(self.classification(), "pre_attach_timeout")

    def test_reserved_timeout_is_classified(self) -> None:
        self.stale_claim()
        self.reserve_attachment()
        self.assertEqual(self.classification(), "reserved_timeout")

    def test_activation_timeout_is_classified(self) -> None:
        self.stale_claim()
        self.reserve_attachment()
        self.assertTrue(
            self.attachments.mark_attached("/vol/user-1.ext4", "operation-1")
        )
        self.save_binding("activating")
        self.assertEqual(self.classification(), "activation_timeout")

    def test_commit_pending_is_classified(self) -> None:
        self.stale_claim()
        self.reserve_attachment()
        self.assertTrue(
            self.attachments.mark_attached("/vol/user-1.ext4", "operation-1")
        )
        self.save_binding("running")
        self.assertEqual(self.classification(), "commit_pending")

    def test_failed_operation_requires_cleanup(self) -> None:
        self.stale_claim()
        self.assertTrue(self.startups.mark_failed("operation-1", "crashed"))
        self.assertEqual(self.classification(), "failed_requires_cleanup")

    def test_succeeded_operation_with_running_binding_needs_commit_repair(self) -> None:
        self.stale_claim()
        self.reserve_attachment()
        self.assertTrue(
            self.attachments.mark_attached("/vol/user-1.ext4", "operation-1")
        )
        self.save_binding("running")
        self.assertTrue(
            self.startups.transition(
                "operation-1",
                "executing",
                "committing",
                runtime_id="runtime-1",
            )
        )
        self.assertTrue(self.startups.mark_succeeded("operation-1"))
        self.assertEqual(self.classification(), "commit_incomplete")


if __name__ == "__main__":
    unittest.main()
