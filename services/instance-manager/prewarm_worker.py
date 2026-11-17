"""预热池后台 Worker 的容量决策层。

阶段 4 的第一步只实现无副作用的补池计划：读取 SQLite 状态快照和 Host 容量，
计算本轮最多可以创建多少台预热机。真实 VMM 创建、定时循环、TTL 回收与 reconcile
将在后续小步接入，避免容量公式和资源操作同时变化。
"""

from __future__ import annotations

from dataclasses import dataclass

from prewarm_config import PrewarmConfig
from prewarm_store import PrewarmStore


@dataclass(frozen=True)
class FillPlan:
    """一次补池决策，不代表已经创建任何真实资源。"""

    create_count: int
    reason: str
    claimable_ready: int
    preparing: int
    claiming: int
    total_public: int


class PrewarmWorker:
    """根据配置、数据库状态和 Host 容量计算补池数量。"""

    def __init__(
        self,
        config: PrewarmConfig,
        *,
        prewarm_store: PrewarmStore,
        profile_key: str,
    ) -> None:
        self.config = config
        self.prewarm_store = prewarm_store
        self.profile_key = profile_key

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
