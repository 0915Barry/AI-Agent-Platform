"""预热项领取与激活编排。

阶段 3A 先把跨表状态变化集中在这里：``PrewarmStore`` 负责预热项状态，
``VolumeAttachmentStore`` 负责数据盘独占，``RuntimeStore`` 负责逻辑实例到物理
runtime 的绑定。真实 Firecracker 热切盘由注入的 ``PrewarmActivator`` 完成。

本模块不负责自动补池；``fill_if_needed``、``drain`` 和 ``reconcile`` 留给阶段 4。
``PREWARM_ENABLED=0`` 时领取直接返回 disabled，不改变任何数据库状态。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

from prewarm_config import PrewarmConfig
from prewarm_store import PrewarmStore
from runtime_store import RuntimeStore, VolumeAttachmentStore


class PrewarmManagerError(RuntimeError):
    """领取、激活或提交过程中无法安全继续。"""


class PrewarmActivator(Protocol):
    """阶段 3B 由真实 runtime 实现的最小激活接口。"""

    def activate_prewarmer(
        self,
        runtime_id: str,
        logical_instance_id: str,
        user_id: str,
        volume_path: str,
        operation_id: str,
        activation_id: str,
    ) -> dict: ...

    def commit_prewarmer(
        self,
        runtime_id: str,
        operation_id: str,
        activation_id: str,
    ) -> None: ...


@dataclass(frozen=True)
class ClaimResult:
    """一次预热领取结果。

    ``claimed=False`` 表示调用方应立即回退冷启动；``claimed=True`` 表示 Guest
    已返回 BOUND_READY，但仍需在业务实例置为 running 后调用 ``commit``。
    """

    claimed: bool
    runtime_id: str | None = None
    activation_id: str | None = None
    operation_id: str | None = None
    runtime: dict | None = None
    reason: str | None = None


class PrewarmManager:
    """单台人工预热接管的状态编排入口。"""

    def __init__(
        self,
        config: PrewarmConfig,
        *,
        prewarm_store: PrewarmStore | None = None,
        attachment_store: VolumeAttachmentStore | None = None,
        runtime_store: RuntimeStore | None = None,
        activator: PrewarmActivator | None = None,
        new_activation_id: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self.prewarm_store = prewarm_store
        self.attachment_store = attachment_store
        self.runtime_store = runtime_store
        self.activator = activator
        self._new_activation_id = new_activation_id or (lambda: uuid.uuid4().hex)

    def target_ready(self) -> int:
        """池目标数量（读取配置）；自动补池阶段才消费该值。"""
        return self.config.target_ready

    def current_ready(self, profile_key: str | None = None) -> int:
        """返回当前 ready 项数量。"""
        if self.prewarm_store is None:
            return 0
        return len(self.prewarm_store.list_ready(profile_key))

    def _require_phase3_dependencies(self) -> tuple[
        PrewarmStore, VolumeAttachmentStore, RuntimeStore, PrewarmActivator
    ]:
        dependencies = (
            self.prewarm_store,
            self.attachment_store,
            self.runtime_store,
            self.activator,
        )
        if any(dependency is None for dependency in dependencies):
            raise PrewarmManagerError("prewarm phase 3 dependencies are not configured")
        return dependencies  # type: ignore[return-value]

    def claim(
        self,
        profile_key: str,
        logical_instance_id: str,
        user_id: str,
        volume_path: str,
        operation_id: str,
    ) -> ClaimResult:
        """原子领取一台 ready 项并完成 Guest 激活。

        没有 ready 项属于正常分支，返回 ``unavailable`` 供 StartupCoordinator
        冷启动兜底。领取后发生任何错误都把预热项和数据盘附件标为 ``error``；
        不自动释放数据盘，避免无法确认 Guest 挂载状态时产生同卷双写。
        """
        if not self.config.enabled:
            return ClaimResult(claimed=False, reason="disabled")

        prewarm_store, attachment_store, runtime_store, activator = (
            self._require_phase3_dependencies()
        )
        activation_id = self._new_activation_id()
        item = prewarm_store.claim(
            profile_key,
            logical_instance_id,
            user_id,
            volume_path,
            operation_id,
            activation_id,
        )
        if item is None:
            return ClaimResult(claimed=False, reason="unavailable")

        runtime_id = str(item["runtime_id"])
        attachment_reserved = False
        try:
            attachment_store.reserve(
                volume_path,
                user_id,
                logical_instance_id,
                runtime_id,
                operation_id,
            )
            attachment_reserved = True
            runtime = activator.activate_prewarmer(
                runtime_id,
                logical_instance_id,
                user_id,
                volume_path,
                operation_id,
                activation_id,
            )
            if not attachment_store.mark_attached(volume_path, operation_id):
                raise PrewarmManagerError("volume attachment generation changed during activation")
            runtime_store.save_binding(
                {
                    "logicalInstanceId": logical_instance_id,
                    "runtimeId": runtime_id,
                    "userId": user_id,
                    "volumePath": volume_path,
                    "activationId": activation_id,
                    "status": "activating",
                }
            )
        except Exception as error:
            message = str(error)[-4000:]
            prewarm_store.mark_error(runtime_id, operation_id, message)
            if attachment_reserved:
                attachment_store.mark_error(volume_path, operation_id, message)
            if isinstance(error, PrewarmManagerError):
                raise
            raise PrewarmManagerError(message) from error

        return ClaimResult(
            claimed=True,
            runtime_id=runtime_id,
            activation_id=activation_id,
            operation_id=operation_id,
            runtime=runtime,
        )

    def commit(
        self,
        logical_instance_id: str,
        runtime_id: str,
        operation_id: str,
        activation_id: str,
    ) -> None:
        """业务实例已置为 running 后提交 Guest 与 Host 的最终归属。"""
        prewarm_store, attachment_store, runtime_store, activator = (
            self._require_phase3_dependencies()
        )
        item = prewarm_store.get(runtime_id)
        if (
            item is None
            or item.get("status") != "claiming"
            or item.get("operation_id") != operation_id
            or item.get("activation_id") != activation_id
            or item.get("logical_instance_id") != logical_instance_id
        ):
            raise PrewarmManagerError("prewarm claim generation changed before commit")

        try:
            activator.commit_prewarmer(runtime_id, operation_id, activation_id)
            binding = runtime_store.get_binding(logical_instance_id)
            if (
                binding is None
                or binding["runtimeId"] != runtime_id
                or binding["activationId"] != activation_id
            ):
                raise PrewarmManagerError("runtime binding changed before prewarm commit")
            binding["status"] = "running"
            runtime_store.save_binding(binding)
            if not prewarm_store.mark_assigned(runtime_id, operation_id):
                raise PrewarmManagerError("prewarm claim generation changed during commit")
        except Exception as error:
            message = str(error)[-4000:]
            prewarm_store.mark_error(runtime_id, operation_id, message)
            attachment_store.mark_error(str(item["volume_path"]), operation_id, message)
            binding = runtime_store.get_binding(logical_instance_id)
            if (
                binding is not None
                and binding["runtimeId"] == runtime_id
                and binding["activationId"] == activation_id
            ):
                binding["status"] = "error"
                runtime_store.save_binding(binding)
            if isinstance(error, PrewarmManagerError):
                raise
            raise PrewarmManagerError(message) from error

    def fill_if_needed(self) -> None:
        """阶段 4 自动补池 Worker 才实现。"""
        raise NotImplementedError("automatic prewarm fill is not implemented yet")

    def drain(self) -> None:
        """阶段 4 自动补池 Worker 才实现。"""
        raise NotImplementedError("automatic prewarm drain is not implemented yet")

    def reconcile(self) -> None:
        """阶段 4 Worker 重启协调才实现。"""
        raise NotImplementedError("prewarm reconcile is not implemented yet")
