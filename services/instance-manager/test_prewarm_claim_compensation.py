#!/usr/bin/env python3
"""尚未接触数据盘的超时领取补偿测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prewarm_claim_audit import PrewarmClaimAuditor
from prewarm_claim_compensation import (
    ActivationTimeoutClaimCompensator,
    ClaimCompensationError,
    PreActivationClaimCompensator,
)
from prewarm_store import PrewarmStore
from runtime_store import RuntimeStore, VolumeAttachmentStore
from startup_store import StartupStore


class FakeRuntime:
    def __init__(self) -> None:
        self.status_value: dict | None = {
            "status": "running",
            "processAlive": True,
            "pid": 1234,
            "processStartTicks": 5678,
        }
        self.stopped: list[tuple[str, str]] = []
        self.destroyed: list[tuple[str, bool]] = []

    def status(self, _instance_id: str) -> dict | None:
        return self.status_value

    def stop(self, instance_id: str, reason: str = "api") -> dict:
        self.stopped.append((instance_id, reason))
        self.status_value = {"status": "stopped", "processAlive": False}
        return {"status": "stopped"}

    def destroy(self, instance_id: str, *, preserve_volume: bool = True) -> None:
        self.destroyed.append((instance_id, preserve_volume))


class PreActivationClaimCompensatorTests(unittest.TestCase):
    NOW = 10_000

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "control-plane.db"
        self.prewarm = PrewarmStore(self.database)
        self.startups = StartupStore(self.database)
        self.attachments = VolumeAttachmentStore(self.database)
        self.runtimes = RuntimeStore(self.database)
        self.runtime = FakeRuntime()
        self.auditor = PrewarmClaimAuditor(
            prewarm_store=self.prewarm,
            startup_store=self.startups,
            attachment_store=self.attachments,
            runtime_store=self.runtimes,
            profile_key="profile-a",
        )
        self.compensator = PreActivationClaimCompensator(
            prewarm_store=self.prewarm,
            startup_store=self.startups,
            attachment_store=self.attachments,
            runtime_store=self.runtimes,
            runtime=self.runtime,
        )
        self.activation_compensator = ActivationTimeoutClaimCompensator(
            prewarm_store=self.prewarm,
            startup_store=self.startups,
            attachment_store=self.attachments,
            runtime_store=self.runtimes,
            runtime=self.runtime,
        )
        self._create_stale_claim()

    def _create_stale_claim(self) -> None:
        self.prewarm.create_item("runtime-1", "profile-a")
        self.assertTrue(self.prewarm.record_runtime_generation("runtime-1", 1234, 5678))
        self.assertTrue(self.prewarm.mark_ready("runtime-1", ttl_seconds=900))
        self.startups.begin("operation-1", "logical-1", "user-1", "prewarm")
        self.assertTrue(self.startups.transition("operation-1", "deciding", "executing"))
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

    def audit(self):
        results = self.auditor.audit(30, now=self.NOW)
        self.assertEqual(len(results), 1)
        return results[0]

    def test_pre_attach_timeout_is_stopped_destroyed_and_retired(self) -> None:
        result = self.compensator.compensate(self.audit())
        self.assertEqual(result.status, "retired")
        self.assertEqual(self.prewarm.get("runtime-1")["status"], "retired")
        self.assertEqual(self.startups.get("operation-1")["status"], "failed")
        self.assertEqual(
            self.runtime.stopped,
            [("runtime-1", "prewarm_claim_timeout_pre_attach")],
        )
        self.assertEqual(self.runtime.destroyed, [("runtime-1", True)])

    def test_missing_runtime_is_destroyed_without_stop(self) -> None:
        self.runtime.status_value = None
        self.compensator.compensate(self.audit())
        self.assertEqual(self.runtime.stopped, [])
        self.assertEqual(self.runtime.destroyed, [("runtime-1", True)])

    def test_generation_mismatch_is_isolated_without_runtime_action(self) -> None:
        self.runtime.status_value = {
            "status": "running",
            "processAlive": True,
            "pid": 9999,
            "processStartTicks": 7777,
        }
        with self.assertRaisesRegex(ClaimCompensationError, "mismatched"):
            self.compensator.compensate(self.audit())
        self.assertEqual(self.startups.get("operation-1")["status"], "compensating")
        self.assertEqual(self.prewarm.get("runtime-1")["status"], "claiming")
        self.assertEqual(self.runtime.stopped, [])

    def test_attachment_race_after_compensation_claim_prevents_cleanup(self) -> None:
        original = self.startups.transition

        def transition_with_race(*args, **kwargs):
            changed = original(*args, **kwargs)
            if changed and args[2] == "compensating":
                self.attachments.reserve(
                    "/vol/user-1.ext4",
                    "user-1",
                    "logical-1",
                    "runtime-1",
                    "operation-1",
                )
            return changed

        with patch.object(self.startups, "transition", side_effect=transition_with_race):
            with self.assertRaisesRegex(ClaimCompensationError, "ownership remains isolated"):
                self.compensator.compensate(self.audit())
        self.assertEqual(self.runtime.stopped, [])
        self.assertEqual(self.prewarm.get("runtime-1")["status"], "claiming")
        self.assertEqual(self.startups.get("operation-1")["status"], "compensating")

    def _reserve_attachment(self) -> None:
        self.attachments.reserve(
            "/vol/user-1.ext4",
            "user-1",
            "logical-1",
            "runtime-1",
            "operation-1",
        )

    def _save_binding(self, status: str) -> None:
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

    def test_reserved_timeout_is_compensated_and_releases_attachment(self) -> None:
        self._reserve_attachment()
        audit = self.auditor.audit(30, now=self.NOW)[0]
        self.assertEqual(audit.classification, "reserved_timeout")

        result = self.compensator.compensate(audit)
        self.assertEqual(result.status, "retired")
        self.assertEqual(self.prewarm.get("runtime-1")["status"], "retired")
        self.assertEqual(self.startups.get("operation-1")["status"], "failed")
        self.assertIsNone(self.attachments.get("/vol/user-1.ext4"))
        self.assertEqual(
            self.runtime.stopped,
            [("runtime-1", "prewarm_claim_timeout_reserved")],
        )
        self.assertEqual(self.runtime.destroyed, [("runtime-1", True)])

    def test_activation_timeout_classification_is_rejected(self) -> None:
        self._reserve_attachment()
        self.attachments.mark_attached("/vol/user-1.ext4", "operation-1")
        self._save_binding("activating")
        audit = self.auditor.audit(30, now=self.NOW)[0]
        self.assertEqual(audit.classification, "activation_timeout")
        with self.assertRaisesRegex(ClaimCompensationError, "not allowed"):
            self.compensator.compensate(audit)
        self.assertEqual(self.runtime.stopped, [])

    def test_reserved_attachment_race_prevents_cleanup(self) -> None:
        self._reserve_attachment()
        original = self.startups.transition

        def transition_with_race(*args, **kwargs):
            changed = original(*args, **kwargs)
            if changed and args[2] == "compensating":
                # 附件从 reserved 越过到 attached，补偿必须放弃清理。
                self.attachments.mark_attached("/vol/user-1.ext4", "operation-1")
            return changed

        with patch.object(self.startups, "transition", side_effect=transition_with_race):
            with self.assertRaisesRegex(
                ClaimCompensationError,
                "reserved volume attachment is missing or changed",
            ):
                self.compensator.compensate(self.auditor.audit(30, now=self.NOW)[0])
        self.assertEqual(self.runtime.stopped, [])
        self.assertEqual(self.prewarm.get("runtime-1")["status"], "claiming")

    def _make_activation_claim(self) -> None:
        self._reserve_attachment()
        self.attachments.mark_attached("/vol/user-1.ext4", "operation-1")
        self._save_binding("activating")

    def test_activation_timeout_is_compensated_and_releases_attachment_and_binding(self) -> None:
        self._make_activation_claim()
        audit = self.auditor.audit(30, now=self.NOW)[0]
        self.assertEqual(audit.classification, "activation_timeout")

        result = self.activation_compensator.compensate(audit)
        self.assertEqual(result.status, "retired")
        self.assertEqual(self.prewarm.get("runtime-1")["status"], "retired")
        self.assertEqual(self.startups.get("operation-1")["status"], "failed")
        self.assertIsNone(self.attachments.get("/vol/user-1.ext4"))
        self.assertIsNone(self.runtimes.get_binding("logical-1"))
        self.assertEqual(
            self.runtime.stopped,
            [("runtime-1", "prewarm_claim_timeout_activation")],
        )
        self.assertEqual(self.runtime.destroyed, [("runtime-1", True)])

    def test_activation_binding_race_prevents_cleanup(self) -> None:
        self._make_activation_claim()
        original = self.startups.transition

        def transition_with_race(*args, **kwargs):
            changed = original(*args, **kwargs)
            if changed and args[2] == "compensating":
                # binding 状态被并发改成 error，越过安全边界。
                binding = self.runtimes.get_binding("logical-1")
                binding["status"] = "error"
                self.runtimes.save_binding(binding)
            return changed

        with patch.object(self.startups, "transition", side_effect=transition_with_race):
            with self.assertRaisesRegex(
                ClaimCompensationError,
                "activating runtime binding is missing or changed",
            ):
                self.activation_compensator.compensate(
                    self.auditor.audit(30, now=self.NOW)[0]
                )
        self.assertEqual(self.runtime.stopped, [])
        self.assertEqual(self.prewarm.get("runtime-1")["status"], "claiming")


if __name__ == "__main__":
    unittest.main()
