"""预热池后台 Worker 的容量决策层。

阶段 4 的第一步只实现无副作用的补池计划：读取 SQLite 状态快照和 Host 容量，
计算本轮最多可以创建多少台预热机。真实 VMM 创建、定时循环、TTL 回收与 reconcile
将在后续小步接入，避免容量公式和资源操作同时变化。
"""

from __future__ import annotations

import hashlib
import secrets
import socket
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

from prewarm_capacity import HostCapacity
from prewarm_config import PrewarmConfig
from prewarm_store import PrewarmStore


class PrewarmWorkerError(RuntimeError):
    """Worker 无法安全完成本轮资源操作。"""


class PrewarmBuilder(Protocol):
    """Worker 创建和补偿清理空白 Guest 所需的最小 Runtime 接口。"""

    def prepare_prewarmer(
        self,
        runtime_id: str,
        claim_token: str,
        *,
        min_free_network_slots: int,
    ) -> dict: ...

    def destroy(self, instance_id: str, *, preserve_volume: bool = True) -> None: ...


class CapacityProbe(Protocol):
    def snapshot(self) -> HostCapacity: ...


@dataclass(frozen=True)
class FillPlan:
    """一次补池决策，不代表已经创建任何真实资源。"""

    create_count: int
    reason: str
    claimable_ready: int
    preparing: int
    claiming: int
    total_public: int


@dataclass(frozen=True)
class FillResult:
    """一次补池巡检已经完成的创建结果。"""

    plan: FillPlan
    created_runtime_ids: tuple[str, ...]


class PrewarmWorker:
    """根据配置、数据库状态和 Host 容量计算补池数量。"""

    def __init__(
        self,
        config: PrewarmConfig,
        *,
        prewarm_store: PrewarmStore,
        profile_key: str,
        builder: PrewarmBuilder | None = None,
        capacity_probe: CapacityProbe | None = None,
        host_id: str | None = None,
        new_runtime_id: Callable[[], str] | None = None,
        new_claim_token: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self.prewarm_store = prewarm_store
        self.profile_key = profile_key
        self.builder = builder
        self.capacity_probe = capacity_probe
        self.host_id = host_id or socket.gethostname()
        self._new_runtime_id = new_runtime_id or (
            lambda: f"prewarm-{uuid.uuid4().hex[:20]}"
        )
        self._new_claim_token = new_claim_token or (
            lambda: secrets.token_urlsafe(32)
        )

    def prepare_one(self) -> dict:
        """创建一台空白 Guest，并在真实 Runtime 就绪后发布为 ``ready``。

        数据库先写 ``preparing``，避免 VMM 已经存在但控制面完全没有记录。原始
        Token 只交给 Runtime，SQLite 仅保存 SHA-256。Runtime 创建失败时其自身
        负责清理未登记资源；若创建成功后的数据库提交失败，Worker 会主动销毁
        已登记 Runtime，禁止产生未受管的空白 Guest。
        """
        if not self.config.enabled:
            raise PrewarmWorkerError("prewarm worker is disabled")
        if self.builder is None:
            raise PrewarmWorkerError("prewarm builder is not configured")

        runtime_id = self._new_runtime_id()
        claim_token = self._new_claim_token()
        token_hash = hashlib.sha256(claim_token.encode("utf-8")).hexdigest()
        self.prewarm_store.create_item(
            runtime_id,
            self.profile_key,
            host_id=self.host_id,
            claim_token_hash=token_hash,
        )

        prepared = False
        try:
            runtime = self.builder.prepare_prewarmer(
                runtime_id,
                claim_token,
                min_free_network_slots=self.config.min_free_network_slots,
            )
            prepared = True
            pid = int(runtime["pid"])
            start_ticks = int(runtime["processStartTicks"])
            if pid <= 0 or start_ticks <= 0:
                raise PrewarmWorkerError("prepared runtime generation is invalid")
            if not self.prewarm_store.record_runtime_generation(
                runtime_id,
                pid,
                start_ticks,
            ):
                raise PrewarmWorkerError("prewarm generation changed before ready")
            if not self.prewarm_store.mark_ready(
                runtime_id,
                ttl_seconds=self.config.ttl_seconds,
                claim_token_hash=token_hash,
            ):
                raise PrewarmWorkerError("prewarm item changed before ready")
        except Exception as error:
            message = str(error)[-4000:]
            self.prewarm_store.mark_prepare_error(runtime_id, message)
            if prepared:
                try:
                    self.builder.destroy(runtime_id, preserve_volume=True)
                except Exception as cleanup_error:
                    raise PrewarmWorkerError(
                        f"{message}; cleanup failed: {str(cleanup_error)[-1000:]}"
                    ) from error
            if isinstance(error, PrewarmWorkerError):
                raise
            raise PrewarmWorkerError(message) from error

        item = self.prewarm_store.get(runtime_id)
        if item is None or item.get("status") != "ready":
            raise PrewarmWorkerError("prepared item disappeared after ready commit")
        return item

    def fill_once(self, *, available_mib: int, free_network_slots: int) -> FillResult:
        """执行一次补池巡检，按同一份计划同步准备所需空白 Guest。

        本方法没有定时循环，也不会自行重试。任一创建失败会立即停止本轮，已经
        成功发布的项保持 ``ready``，失败项由 ``prepare_one`` 记录为 ``error``；
        上层 Worker 服务后续可据此执行退避，避免故障期间持续创建资源。
        """
        plan = self.plan_fill(
            available_mib=available_mib,
            free_network_slots=free_network_slots,
        )
        created: list[str] = []
        for _ in range(plan.create_count):
            item = self.prepare_one()
            created.append(str(item["runtime_id"]))
        return FillResult(plan=plan, created_runtime_ids=tuple(created))

    def fill_once_from_host(self) -> FillResult:
        """读取一次真实 Host 容量快照并执行单轮补池。"""
        if self.capacity_probe is None:
            raise PrewarmWorkerError("prewarm capacity probe is not configured")
        capacity = self.capacity_probe.snapshot()
        return self.fill_once(
            available_mib=capacity.available_mib,
            free_network_slots=capacity.free_network_slots,
        )

    def plan_fill(self, *, available_mib: int, free_network_slots: int) -> FillPlan:
        """计算本轮创建数，不写数据库、不启动 microVM。

        ``claiming`` 已不可供下一位用户领取，所以不抵消 ready 缺额；但它仍占用
        公共池总量和网络资源，因此计入 ``max_total``。已过期 ready 同理只计入
        总量，等待后续回收步骤释放。
        """
        counts = self.prewarm_store.capacity_counts(self.profile_key)
        preparing = counts["preparing"]
        claimable_ready = counts["claimable_ready"]
        claiming = counts["claiming"]
        total_public = preparing + counts["ready_total"] + claiming

        if not self.config.enabled:
            return self._plan(0, "disabled", counts, total_public)

        ready_deficit = max(
            0,
            self.config.target_ready - (claimable_ready + preparing),
        )
        total_room = max(0, self.config.max_total - total_public)
        concurrency_room = max(0, self.config.build_concurrency - preparing)
        network_room = max(
            0,
            free_network_slots - self.config.min_free_network_slots,
        )

        if available_mib < self.config.min_available_mib:
            return self._plan(0, "memory_guard", counts, total_public)

        create_count = min(
            ready_deficit,
            total_room,
            concurrency_room,
            network_room,
        )
        if create_count > 0:
            reason = "fill"
        elif ready_deficit == 0:
            reason = "target_satisfied"
        elif total_room == 0:
            reason = "max_total"
        elif concurrency_room == 0:
            reason = "build_concurrency"
        else:
            reason = "network_guard"
        return self._plan(create_count, reason, counts, total_public)

    @staticmethod
    def _plan(
        create_count: int,
        reason: str,
        counts: dict[str, int],
        total_public: int,
    ) -> FillPlan:
        return FillPlan(
            create_count=create_count,
            reason=reason,
            claimable_ready=counts["claimable_ready"],
            preparing=counts["preparing"],
            claiming=counts["claiming"],
            total_public=total_public,
        )
