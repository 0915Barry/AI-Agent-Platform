"""超时 ``claiming`` 的只读审计与补偿前分类。"""

from __future__ import annotations

from dataclasses import dataclass

from prewarm_store import PrewarmStore
from runtime_store import RuntimeStore, VolumeAttachmentStore
from startup_store import StartupStore


@dataclass(frozen=True)
class StaleClaimAudit:
    runtime_id: str
    logical_instance_id: str
    operation_id: str
    classification: str
    operation_status: str | None
    attachment_status: str | None
    binding_status: str | None


class PrewarmClaimAuditor:
    """组合四张状态表，判断超时领取停在哪个安全边界。"""

    def __init__(
        self,
        *,
        prewarm_store: PrewarmStore,
        startup_store: StartupStore,
        attachment_store: VolumeAttachmentStore,
        runtime_store: RuntimeStore,
        profile_key: str,
    ) -> None:
        self.prewarm_store = prewarm_store
        self.startup_store = startup_store
        self.attachment_store = attachment_store
        self.runtime_store = runtime_store
        self.profile_key = profile_key

    def audit(self, timeout_seconds: int, *, now: int | None = None) -> tuple[StaleClaimAudit, ...]:
        results = [
            self._classify(item)
            for item in self.prewarm_store.list_stale_claiming(
                self.profile_key,
                timeout_seconds,
                now=now,
            )
        ]
        return tuple(results)

    def _classify(self, item: dict) -> StaleClaimAudit:
        runtime_id = str(item["runtime_id"])
        logical_id = str(item.get("logical_instance_id") or "")
        operation_id = str(item.get("operation_id") or "")
        volume_path = str(item.get("volume_path") or "")
        operation = self.startup_store.get(operation_id) if operation_id else None
        attachment = self.attachment_store.get(volume_path) if volume_path else None
        binding = self.runtime_store.get_binding(logical_id) if logical_id else None
        operation_status = None if operation is None else str(operation["status"])
        attachment_status = None if attachment is None else str(attachment["status"])
        binding_status = None if binding is None else str(binding["status"])

        if operation is None:
            classification = "operation_missing"
        elif (
            operation.get("strategy") != "prewarm"
            or operation.get("logical_instance_id") != logical_id
            or operation.get("operation_id") != operation_id
            or (operation.get("runtime_id") not in {None, runtime_id})
        ):
            classification = "operation_mismatch"
        elif operation_status == "failed":
            classification = "failed_requires_cleanup"
        elif operation_status == "succeeded":
            if attachment_status == "attached" and binding_status == "running":
                classification = "commit_incomplete"
            else:
                classification = "terminal_inconsistent"
        elif attachment_status == "error" or binding_status == "error":
            classification = "attachment_error"
        elif attachment is None and binding is None:
            classification = "pre_attach_timeout"
        elif attachment_status == "reserved" and binding is None:
            classification = "reserved_timeout"
        elif attachment_status == "attached" and binding_status == "activating":
            classification = "activation_timeout"
        elif attachment_status == "attached" and binding_status == "running":
            classification = "commit_pending"
        else:
            classification = "inconsistent"

        return StaleClaimAudit(
            runtime_id=runtime_id,
            logical_instance_id=logical_id,
            operation_id=operation_id,
            classification=classification,
            operation_status=operation_status,
            attachment_status=attachment_status,
            binding_status=binding_status,
        )
