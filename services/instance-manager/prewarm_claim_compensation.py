"""对尚未确认 Guest 挂载用户盘的超时预热领取执行安全补偿。"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Protocol

from prewarm_claim_audit import StaleClaimAudit
from prewarm_store import PrewarmStore
from runtime_store import RuntimeStore, VolumeAttachmentStore
from startup_store import ACTIVE_STARTUP_STATUSES, StartupStore


class ClaimCompensationError(RuntimeError):
    """超时领取无法在当前状态下安全补偿。"""


class ClaimRuntime(Protocol):
    def status(self, instance_id: str) -> dict | None: ...

    def stop(self, instance_id: str, reason: str = "api") -> dict: ...

    def destroy(self, instance_id: str, *, preserve_volume: bool = True) -> None: ...


@dataclass(frozen=True)
class ClaimCompensationResult:
    runtime_id: str
    operation_id: str
    status: str


class PreActivationClaimCompensator:
    """处理尚未确认 Guest 挂载用户盘的超时领取：``pre_attach_timeout`` 与 ``reserved_timeout``。

    - ``pre_attach_timeout``：没有 ``volume_attachments``，Guest 一定还停在占位盘；
    - ``reserved_timeout``：``volume_attachments`` 已 ``reserved`` 但未 ``attached``，
      Guest 可能已被 PATCH 切到用户盘。

    两者都停在「确认挂载」这个安全边界之前，因此统一按「停 VMM → 销毁运行时 →
    释放 reserved 附件（若存在）→ 退休预热项 → 结束启动操作」回滚；区别只在
    是否释放 reserved 附件。``activation_timeout`` / ``commit_pending`` 已确认
    挂载或已提交，不在这里处理。
    """

    SUPPORTED_CLASSIFICATIONS = ("pre_attach_timeout", "reserved_timeout")

    def __init__(
        self,
        *,
        prewarm_store: PrewarmStore,
        startup_store: StartupStore,
        attachment_store: VolumeAttachmentStore,
        runtime_store: RuntimeStore,
        runtime: ClaimRuntime,
    ) -> None:
        self.prewarm_store = prewarm_store
        self.startup_store = startup_store
        self.attachment_store = attachment_store
        self.runtime_store = runtime_store
        self.runtime = runtime

    def compensate(self, audit: StaleClaimAudit) -> ClaimCompensationResult:
        if audit.classification not in self.SUPPORTED_CLASSIFICATIONS:
            raise ClaimCompensationError(
                f"automatic compensation is not allowed for {audit.classification}"
            )
        operation = self.startup_store.get(audit.operation_id)
        if operation is None or operation.get("status") not in ACTIVE_STARTUP_STATUSES:
            raise ClaimCompensationError("startup operation is no longer active")

        operation_status = str(operation["status"])
        if operation_status != "compensating" and not self.startup_store.transition(
            audit.operation_id,
            operation_status,
            "compensating",
            runtime_id=audit.runtime_id,
            error="stale pre-activation prewarm claim",
        ):
            raise ClaimCompensationError("startup operation changed before compensation")

        item = self.prewarm_store.get(audit.runtime_id)
        operation = self.startup_store.get(audit.operation_id)
        attachment = (
            self.attachment_store.get(str(item.get("volume_path") or ""))
            if item is not None
            else None
        )
        binding = self.runtime_store.get_binding(audit.logical_instance_id)
        if (
            item is None
            or item.get("status") != "claiming"
            or item.get("operation_id") != audit.operation_id
            or item.get("logical_instance_id") != audit.logical_instance_id
            or operation is None
            or operation.get("status") != "compensating"
            or binding is not None
        ):
            raise ClaimCompensationError(
                "claim state changed after entering compensation; ownership remains isolated"
            )

        attachment_status = None if attachment is None else str(attachment.get("status"))
        if audit.classification == "pre_attach_timeout":
            if attachment is not None:
                raise ClaimCompensationError(
                    "volume attachment appeared after compensation; ownership remains isolated"
                )
        elif attachment is None or attachment_status != "reserved":
            raise ClaimCompensationError(
                "reserved volume attachment is missing or changed; ownership remains isolated"
            )

        runtime_status = self.runtime.status(audit.runtime_id)
        if runtime_status is not None and runtime_status.get("processAlive") is True:
            if not self._generation_matches(item, runtime_status):
                raise ClaimCompensationError(
                    "refusing to stop mismatched prewarm runtime generation"
                )
            reason = (
                "prewarm_claim_timeout_pre_attach"
                if audit.classification == "pre_attach_timeout"
                else "prewarm_claim_timeout_reserved"
            )
            stopped = self.runtime.stop(audit.runtime_id, reason=reason)
            if stopped.get("status") != "stopped":
                raise ClaimCompensationError("pre-activation runtime did not stop")
        print(
            "PREWARM_DESTRUCTIVE_ACTION actor=claim-compensator "
            f"classification={audit.classification} action=destroy-runtime "
            f"logical={audit.logical_instance_id} runtime={audit.runtime_id} "
            f"operation={audit.operation_id}",
            file=sys.stderr,
            flush=True,
        )
        self.runtime.destroy(audit.runtime_id, preserve_volume=True)

        if audit.classification == "reserved_timeout":
            if not self.attachment_store.release(
                str(item["volume_path"]), audit.operation_id
            ):
                raise ClaimCompensationError(
                    "failed to release reserved volume attachment after runtime cleanup"
                )

        if not self.prewarm_store.mark_error(
            audit.runtime_id,
            audit.operation_id,
            "stale pre-activation prewarm claim",
        ):
            raise ClaimCompensationError("claim generation changed after runtime cleanup")
        if not self.prewarm_store.mark_draining(audit.runtime_id):
            raise ClaimCompensationError("failed to enter draining after claim cleanup")
        if not self.prewarm_store.retire(audit.runtime_id):
            raise ClaimCompensationError("failed to retire compensated prewarm item")
        if not self.startup_store.mark_failed(
            audit.operation_id,
            "stale pre-activation prewarm claim was cleaned",
        ):
            raise ClaimCompensationError("failed to finalize compensated startup operation")
        return ClaimCompensationResult(
            runtime_id=audit.runtime_id,
            operation_id=audit.operation_id,
            status="retired",
        )

    @staticmethod
    def _generation_matches(item: dict, runtime_status: dict) -> bool:
        expected_pid = item.get("vmm_pid")
        expected_ticks = item.get("process_start_ticks")
        if expected_pid is None or expected_ticks is None:
            return False
        return (
            int(runtime_status.get("pid", -1)) == int(expected_pid)
            and int(runtime_status.get("processStartTicks", -1)) == int(expected_ticks)
        )


class ActivationTimeoutClaimCompensator:
    """处理已确认挂载但从未提交的 ``activation_timeout`` 领取。

    Guest 已挂载用户盘、binding 已建立为 ``activating``，但 commit 从未发生。
    补偿是完整回滚：停 VMM → 销毁运行时 → 释放 ``attached`` 附件 → 删除 binding
    → 退休预热项 → 结束启动操作。附件与 binding 必须在 VMM 停止后释放，避免
    用户盘仍被挂载时就解锁给其他实例。
    """

    def __init__(
        self,
        *,
        prewarm_store: PrewarmStore,
        startup_store: StartupStore,
        attachment_store: VolumeAttachmentStore,
        runtime_store: RuntimeStore,
        runtime: ClaimRuntime,
    ) -> None:
        self.prewarm_store = prewarm_store
        self.startup_store = startup_store
        self.attachment_store = attachment_store
        self.runtime_store = runtime_store
        self.runtime = runtime

    def compensate(self, audit: StaleClaimAudit) -> ClaimCompensationResult:
        if audit.classification != "activation_timeout":
            raise ClaimCompensationError(
                f"automatic compensation is not allowed for {audit.classification}"
            )
        operation = self.startup_store.get(audit.operation_id)
        if operation is None or operation.get("status") not in ACTIVE_STARTUP_STATUSES:
            raise ClaimCompensationError("startup operation is no longer active")

        operation_status = str(operation["status"])
        if operation_status != "compensating" and not self.startup_store.transition(
            audit.operation_id,
            operation_status,
            "compensating",
            runtime_id=audit.runtime_id,
            error="stale activation prewarm claim",
        ):
            raise ClaimCompensationError("startup operation changed before compensation")

        item = self.prewarm_store.get(audit.runtime_id)
        operation = self.startup_store.get(audit.operation_id)
        attachment = (
            self.attachment_store.get(str(item.get("volume_path") or ""))
            if item is not None
            else None
        )
        binding = self.runtime_store.get_binding(audit.logical_instance_id)
        if (
            item is None
            or item.get("status") != "claiming"
            or item.get("operation_id") != audit.operation_id
            or item.get("logical_instance_id") != audit.logical_instance_id
            or operation is None
            or operation.get("status") != "compensating"
        ):
            raise ClaimCompensationError(
                "claim state changed after entering compensation; ownership remains isolated"
            )
        if attachment is None or str(attachment.get("status")) != "attached":
            raise ClaimCompensationError(
                "attached volume attachment is missing or changed; ownership remains isolated"
            )
        if (
            binding is None
            or binding.get("runtimeId") != audit.runtime_id
            or binding.get("status") != "activating"
            or binding.get("activationId") != str(item.get("activation_id"))
        ):
            raise ClaimCompensationError(
                "activating runtime binding is missing or changed; ownership remains isolated"
            )

        runtime_status = self.runtime.status(audit.runtime_id)
        if runtime_status is not None and runtime_status.get("processAlive") is True:
            if not self._generation_matches(item, runtime_status):
                raise ClaimCompensationError(
                    "refusing to stop mismatched prewarm runtime generation"
                )
            stopped = self.runtime.stop(
                audit.runtime_id, reason="prewarm_claim_timeout_activation"
            )
            if stopped.get("status") != "stopped":
                raise ClaimCompensationError("activation runtime did not stop")
        print(
            "PREWARM_DESTRUCTIVE_ACTION actor=activation-compensator "
            f"classification={audit.classification} action=destroy-runtime "
            f"logical={audit.logical_instance_id} runtime={audit.runtime_id} "
            f"operation={audit.operation_id}",
            file=sys.stderr,
            flush=True,
        )
        self.runtime.destroy(audit.runtime_id, preserve_volume=True)

        if not self.attachment_store.release(
            str(item["volume_path"]), audit.operation_id
        ):
            raise ClaimCompensationError(
                "failed to release attached volume attachment after runtime cleanup"
            )
        print(
            "PREWARM_DESTRUCTIVE_ACTION actor=activation-compensator "
            f"classification={audit.classification} action=delete-binding "
            f"logical={audit.logical_instance_id} runtime={audit.runtime_id} "
            f"operation={audit.operation_id}",
            file=sys.stderr,
            flush=True,
        )
        self.runtime_store.delete_binding(audit.logical_instance_id)

        if not self.prewarm_store.mark_error(
            audit.runtime_id,
            audit.operation_id,
            "stale activation prewarm claim",
        ):
            raise ClaimCompensationError("claim generation changed after runtime cleanup")
        if not self.prewarm_store.mark_draining(audit.runtime_id):
            raise ClaimCompensationError("failed to enter draining after claim cleanup")
        if not self.prewarm_store.retire(audit.runtime_id):
            raise ClaimCompensationError("failed to retire compensated prewarm item")
        if not self.startup_store.mark_failed(
            audit.operation_id,
            "stale activation prewarm claim was cleaned",
        ):
            raise ClaimCompensationError("failed to finalize compensated startup operation")
        return ClaimCompensationResult(
            runtime_id=audit.runtime_id,
            operation_id=audit.operation_id,
            status="retired",
        )

    @staticmethod
    def _generation_matches(item: dict, runtime_status: dict) -> bool:
        expected_pid = item.get("vmm_pid")
        expected_ticks = item.get("process_start_ticks")
        if expected_pid is None or expected_ticks is None:
            return False
        return (
            int(runtime_status.get("pid", -1)) == int(expected_pid)
            and int(runtime_status.get("processStartTicks", -1)) == int(expected_ticks)
        )


class CommitPendingClaimCompensator:
    """处理已提交但最后一笔记账缺失的 ``commit_pending`` 领取（向前修复）。

    Guest 已挂载用户盘、binding 已 ``running``，只差 item 未置 ``assigned``。
    补偿不碰任何运行中的资源：不停 VMM、不释放附件、不删 binding，只补两笔
    条件写——item ``claiming→assigned``、操作 ``committing→succeeded``。
    """

    def __init__(
        self,
        *,
        prewarm_store: PrewarmStore,
        startup_store: StartupStore,
        attachment_store: VolumeAttachmentStore,
        runtime_store: RuntimeStore,
    ) -> None:
        self.prewarm_store = prewarm_store
        self.startup_store = startup_store
        self.attachment_store = attachment_store
        self.runtime_store = runtime_store

    def compensate(self, audit: StaleClaimAudit) -> ClaimCompensationResult:
        if audit.classification != "commit_pending":
            raise ClaimCompensationError(
                f"automatic compensation is not allowed for {audit.classification}"
            )
        operation = self.startup_store.get(audit.operation_id)
        if operation is None or operation.get("status") != "committing":
            raise ClaimCompensationError("startup operation is not committing")

        item = self.prewarm_store.get(audit.runtime_id)
        attachment = (
            self.attachment_store.get(str(item.get("volume_path") or ""))
            if item is not None
            else None
        )
        binding = self.runtime_store.get_binding(audit.logical_instance_id)
        if (
            item is None
            or item.get("status") != "claiming"
            or item.get("operation_id") != audit.operation_id
            or item.get("logical_instance_id") != audit.logical_instance_id
        ):
            raise ClaimCompensationError(
                "claim state changed before commit repair; ownership remains isolated"
            )
        if attachment is None or str(attachment.get("status")) != "attached":
            raise ClaimCompensationError(
                "attached volume attachment is missing or changed; ownership remains isolated"
            )
        if (
            binding is None
            or binding.get("runtimeId") != audit.runtime_id
            or binding.get("status") != "running"
            or binding.get("activationId") != str(item.get("activation_id"))
        ):
            raise ClaimCompensationError(
                "running runtime binding is missing or changed; ownership remains isolated"
            )

        if not self.prewarm_store.mark_assigned(audit.runtime_id, audit.operation_id):
            raise ClaimCompensationError("claim generation changed during commit repair")
        if not self.startup_store.mark_succeeded(audit.operation_id):
            raise ClaimCompensationError("startup operation changed during commit repair")
        return ClaimCompensationResult(
            runtime_id=audit.runtime_id,
            operation_id=audit.operation_id,
            status="assigned",
        )
