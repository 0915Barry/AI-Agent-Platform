#!/usr/bin/env python3
"""统一启动协调器（阶段 2）。

把「如何启动一台实例」的执行与代次记录收口到一处，用 ``startup_operations``
记录每次操作的代次。阶段 2 只路由冷启动与快照恢复；预热领取留待阶段 3 接入。

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


class StartupCoordinator:
    def __init__(
        self,
        startup_store: StartupStore,
        runtime: RuntimeLike,
        *,
        new_operation_id: Callable[[], str] | None = None,
    ) -> None:
        self.startup_store = startup_store
        self.runtime = runtime
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
        return StartupResult(operation_id=operation_id, strategy=strategy, runtime=runtime)

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

    def commit_success(self, operation_id: str) -> None:
        """调用方已把业务实例置为 running 后调用，结束本次操作。"""
        self.startup_store.mark_succeeded(operation_id)

    def commit_failure(self, operation_id: str, error: str) -> None:
        """调用方回滚业务状态后调用，结束本次操作并记录失败。"""
        self.startup_store.mark_failed(operation_id, error)
