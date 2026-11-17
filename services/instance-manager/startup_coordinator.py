#!/usr/bin/env python3
"""统一启动协调器。

把「如何启动一台实例」的执行与代次记录收口到一处，用 ``startup_operations``
记录每次操作的代次，并在启用预热时执行“预热优先、冷启动兜底”。

调用方（ControlPlane）负责在前后维护业务实例状态
（``starting/resuming → running/failed``），本协调器只负责：

- 生成 ``operation_id`` 并登记 ``startup_operations``；
- 记录 ``deciding → executing → committing``；
- 执行 runtime 调用；
- 失败时标记 ``failed`` 并上抛（由调用方决定如何回滚业务状态）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

from prewarm_manager import ClaimResult, PrewarmManager
from startup_store import StartupStore


class RuntimeLike(Protocol):
    """协调器需要的最小 runtime 接口，便于用 fake 做单测。"""

    def start(self, instance_id: str, owner_id: str | None) -> dict: ...
    def resume(self, instance_id: str, owner_id: str | None) -> dict: ...


@dataclass(frozen=True)
class StartupResult:
    """一次启动操作已进入 committing（runtime 成功、业务状态待提交）的结果。"""

    operation_id: str
    strategy: str
    runtime: dict
    instance_id: str
    runtime_id: str | None = None
    activation_id: str | None = None


class StartupCoordinator:
    def __init__(
        self,
        startup_store: StartupStore,
        runtime: RuntimeLike,
        *,
        prewarm_manager: PrewarmManager | None = None,
        new_operation_id: Callable[[], str] | None = None,
    ) -> None:
        self.startup_store = startup_store
        self.runtime = runtime
        self.prewarm_manager = prewarm_manager
        self._new_operation_id = new_operation_id or (lambda: uuid.uuid4().hex)

    def _run(
        self,
        strategy: str,
        instance_id: str,
        owner_id: str | None,
        execute: Callable[[], dict],
    ) -> StartupResult:
        operation_id = self._new_operation_id()
        self.startup_store.begin(operation_id, instance_id, owner_id or "", strategy)
        self.startup_store.transition(operation_id, "deciding", "executing")
        try:
            runtime = execute()
        except Exception as error:
            self.startup_store.mark_failed(operation_id, str(error)[-4000:])
            raise
        self.startup_store.transition(operation_id, "executing", "committing")
        return StartupResult(
            operation_id=operation_id,
            strategy=strategy,
            runtime=runtime,
            instance_id=instance_id,
        )

    def _run_prewarm(
        self,
        instance_id: str,
        owner_id: str,
        volume_path: str,
        profile_key: str,
    ) -> StartupResult | None:
        """尝试原子领取预热项；没有 ready 项时返回 ``None``。

        只有明确的 ``unavailable`` 才允许调用方回退冷启动。领取后若激活失败，
        数据盘是否已被 Guest 打开无法确定，因此错误必须上抛，不能再启动第二台
        microVM 挂载同一块盘。
        """
        manager = self.prewarm_manager
        if manager is None or not manager.config.enabled:
            return None

        operation_id = self._new_operation_id()
        self.startup_store.begin(operation_id, instance_id, owner_id, "prewarm")
        self.startup_store.transition(operation_id, "deciding", "executing")
        try:
            claim: ClaimResult = manager.claim(
                profile_key,
                instance_id,
                owner_id,
                volume_path,
                operation_id,
            )
        except Exception as error:
            self.startup_store.mark_failed(operation_id, str(error)[-4000:])
            raise
        if not claim.claimed:
            self.startup_store.mark_failed(
                operation_id,
                f"prewarm {claim.reason or 'unavailable'}; cold fallback",
            )
            return None
        if (
            claim.runtime is None
            or claim.runtime_id is None
            or claim.activation_id is None
        ):
            error = "claimed prewarm result is incomplete"
            self.startup_store.mark_failed(operation_id, error)
            raise RuntimeError(error)
        if not self.startup_store.transition(
            operation_id,
            "executing",
            "committing",
            runtime_id=claim.runtime_id,
        ):
            raise RuntimeError("prewarm startup generation changed before commit")
        return StartupResult(
            operation_id=operation_id,
            strategy="prewarm",
            runtime=claim.runtime,
            instance_id=instance_id,
            runtime_id=claim.runtime_id,
            activation_id=claim.activation_id,
        )

    def run_start(
        self,
        instance_id: str,
        owner_id: str | None,
        *,
        volume_path: str | None = None,
        profile_key: str = "default-2vcpu-1g",
    ) -> StartupResult:
        """优先领取预热项；关闭、无属主或池为空时执行原冷启动。"""
        if owner_id is not None and volume_path is not None:
            claimed = self._run_prewarm(
                instance_id,
                owner_id,
                volume_path,
                profile_key,
            )
            if claimed is not None:
                return claimed
        return self.run_cold(instance_id, owner_id)

    def run_cold(self, instance_id: str, owner_id: str | None) -> StartupResult:
        """记录并执行冷启动。"""
        return self._run(
            "cold",
            instance_id,
            owner_id,
            lambda: self.runtime.start(instance_id, owner_id),
        )

    def run_snapshot(self, instance_id: str, owner_id: str | None) -> StartupResult:
        """记录并执行快照恢复。"""
        return self._run(
            "snapshot",
            instance_id,
            owner_id,
            lambda: self.runtime.resume(instance_id, owner_id),
        )

    def commit_success(self, operation: str | StartupResult) -> None:
        """业务实例置为 running 后提交 Guest 归属并结束本次操作。"""
        if isinstance(operation, str):
            operation_id = operation
        else:
            operation_id = operation.operation_id
            if operation.strategy == "prewarm":
                if (
                    self.prewarm_manager is None
                    or operation.runtime_id is None
                    or operation.activation_id is None
                ):
                    raise RuntimeError("prewarm commit context is incomplete")
                try:
                    self.prewarm_manager.commit(
                        operation.instance_id,
                        operation.runtime_id,
                        operation.operation_id,
                        operation.activation_id,
                    )
                except Exception as error:
                    self.startup_store.mark_failed(operation_id, str(error)[-4000:])
                    raise
        if not self.startup_store.mark_succeeded(operation_id):
            raise RuntimeError("startup operation is not in committing state")

    def commit_failure(self, operation_id: str, error: str) -> None:
        """调用方回滚业务状态后调用，结束本次操作并记录失败。"""
        self.startup_store.mark_failed(operation_id, error)

    def prewarm_assignment(self, instance_id: str) -> dict | None:
        """查询实例是否由预热 Runtime 承载，与当前开关状态无关。"""
        if self.prewarm_manager is None:
            return None
        return self.prewarm_manager.assigned_context(instance_id)

    def complete_prewarm_stop(self, instance_id: str) -> dict | None:
        """物理 Runtime 已停止后释放本次预热归属。"""
        if self.prewarm_manager is None:
            return None
        return self.prewarm_manager.complete_logical_stop(instance_id)
