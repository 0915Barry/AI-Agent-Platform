"""预热池管理器接口：ControlPlane 与 pool worker 都通过它操作。

阶段 0 只定义接口，不接真实资源。``PREWARM_ENABLED=0`` 时对现有系统零影响。
后续改数量/策略只改配置或本接口实现，编排核心（ControlPlane/worker）不感知。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from prewarm_config import PrewarmConfig


@dataclass(frozen=True)
class ClaimResult:
    """一次领取的结果；阶段 3 才真正使用。"""

    claimed: bool
    runtime_id: str | None = None
    reason: str | None = None


class PrewarmManager:
    """空白 Guest 池的唯一入口。"""

    def __init__(self, config: PrewarmConfig) -> None:
        self.config = config

    def target_ready(self) -> int:
        """池目标数量（读配置）；改数量 = 改 config/prewarm.env。"""
        return self.config.target_ready

    def current_ready(self) -> int:
        """当前可领取数量；阶段 0 还没有池，恒为 0。"""
        return 0

    def fill_if_needed(self) -> None:
        """缺额则补池；阶段 0 未实现。"""
        raise NotImplementedError("prewarm fill is not implemented yet")

    def drain(self) -> None:
        """停止接纳新领取、清理未分配项；阶段 0 未实现。"""
        raise NotImplementedError("prewarm drain is not implemented yet")

    def claim(
        self,
        profile: Any,
        logical_instance_id: str,
        user_id: str,
        volume_path: str,
    ) -> ClaimResult:
        """原子领取一台 ready 项；阶段 0 未实现。"""
        raise NotImplementedError("prewarm claim is not implemented yet")

    def reconcile(self) -> None:
        """重启后核对物理资源与状态；阶段 0 未实现。"""
        raise NotImplementedError("prewarm reconcile is not implemented yet")
