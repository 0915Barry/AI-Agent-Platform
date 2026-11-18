#!/usr/bin/env python3
"""阶段 6.1：跨策略统一操作计时。

``startupOperationTiming`` 是冷启动与预热命中共用的端到端计时口径。它与既有的
``startupTiming``（Runtime/Guest 内部诊断）并存，不改变后者的含义。

本模块只负责**纯数据结构与计算**：定义版本化结构、合并各层局部 phase 报告、
计算 ``unattributedMs`` 余数并校验不变量。它不读数据库、不起进程，也不被任何
单一层级用作最终 ``totalMs`` 的来源——最终 ``totalMs`` 只能由 Control Plane 生成。
"""

from __future__ import annotations

STARTUP_OPERATION_TIMING_VERSION = 1

STRATEGY_COLD = "cold"
STRATEGY_PREWARM = "prewarm"
STRATEGY_SNAPSHOT = "snapshot"
KNOWN_STRATEGIES = (STRATEGY_COLD, STRATEGY_PREWARM, STRATEGY_SNAPSHOT)

# 九个业务阶段，互不重叠的增量。不适用的阶段固定为 0，不能省略。
# ``unattributedMs`` 是余数，不在本清单内，由 Control Plane 最终计算。
PHASES = (
    "decisionMs",
    "claimMs",
    "volumePrepareMs",
    "networkReadyMs",
    "gatewayReadyMs",
    "runtimeReadyMs",
    "drivePatchMs",
    "guestActivateMs",
    "commitMs",
)

# 里程碑：从统一操作起点开始的累计偏移，不参与 phase 求和。
MILESTONES = ("guestDeclaredRpcReadyMs",)

# 各 phase 独立取整到毫秒，跨 9 段累计的舍入误差允许在几毫秒内。
ROUNDING_TOLERANCE_MS = 5

# 冷启动旧 ``startupTiming.host`` 阶段到统一 phase 的映射。
# ``ensure_host_ms`` 有意不在此表：它属于 Host 前置开销，归入 unattributedMs。
COLD_HOST_STAGE_TO_PHASE = {
    "volume_create_ms": "volumePrepareMs",
    "e2fsck_ms": "volumePrepareMs",
    "volume_config_ms": "volumePrepareMs",
    "network_allocate_ms": "networkReadyMs",
    "network_setup_ms": "networkReadyMs",
    "gateway_ready_ms": "gatewayReadyMs",
    "jail_prepare_ms": "runtimeReadyMs",
    "guest_ready_ms": "runtimeReadyMs",
    "lifecycle_ms": "runtimeReadyMs",
}


class StartupOperationTimingError(ValueError):
    """计时结构违反不变量，或输入包含未知/负数/重复字段。"""


def map_cold_host_stages(host_stages: dict[str, int] | None) -> dict[str, int]:
    """把冷启动旧 ``host`` 阶段映射为 Runtime 拥有的统一 phase。

    只返回冷启动 Runtime 自己产出的四项 phase；``ensure_host_ms`` 及未知阶段不映射，
    留作 Control Plane 计算的 ``unattributedMs`` 余数。旧 ``startupTiming.guest`` 是
    Guest 内部诊断，与 ``guest_ready_ms`` 重叠，不参与 phase 映射以免重复计入。
    """
    phases = {
        "volumePrepareMs": 0,
        "networkReadyMs": 0,
        "gatewayReadyMs": 0,
        "runtimeReadyMs": 0,
    }
    for stage, value in (host_stages or {}).items():
        target = COLD_HOST_STAGE_TO_PHASE.get(stage)
        if target is not None:
            phases[target] += int(value)
    return phases


def merge_phases(*reports: dict[str, int] | None) -> dict[str, int]:
    """合并各层上报的局部 phase 增量。

    同一 phase 被两层同时上报（重叠）或出现未知 phase 都视为错误；负数直接拒绝。
    ``None`` 或空报告会被跳过，用于表示「本策略不适用、未产生任何 phase」。
    """
    merged: dict[str, int] = {}
    for report in reports:
        if not report:
            continue
        for name, value in report.items():
            if name not in PHASES:
                raise StartupOperationTimingError(f"unknown phase: {name}")
            if name in merged:
                raise StartupOperationTimingError(f"duplicate phase report: {name}")
            value = int(value)
            if value < 0:
                raise StartupOperationTimingError(f"negative phase value: {name}={value}")
            merged[name] = value
    return merged


def compute_unattributed(total_ms: int, phases: dict[str, int]) -> int:
    """余数 = totalMs 减去已知 phase 之和，下限为 0。"""
    known = sum(int(phases.get(name, 0)) for name in PHASES)
    return max(0, int(total_ms) - known)


class StartupOperationTiming:
    """一次启动操作的统一端到端计时（不可变值对象）。"""

    def __init__(
        self,
        *,
        strategy: str,
        total_ms: int,
        phases: dict[str, int],
        milestones: dict[str, int] | None = None,
    ) -> None:
        if strategy not in KNOWN_STRATEGIES:
            raise StartupOperationTimingError(f"unknown strategy: {strategy}")
        total_ms = int(total_ms)
        if total_ms < 0:
            raise StartupOperationTimingError(f"negative totalMs: {total_ms}")

        normalized = {name: 0 for name in PHASES}
        for name, value in (phases or {}).items():
            if name not in PHASES:
                raise StartupOperationTimingError(f"unknown phase: {name}")
            value = int(value)
            if value < 0:
                raise StartupOperationTimingError(f"negative phase: {name}={value}")
            normalized[name] = value

        self.strategy = strategy
        self.total_ms = total_ms
        self.phases = normalized  # 仅九个业务阶段，不含 unattributedMs
        self.unattributed_ms = compute_unattributed(total_ms, normalized)
        self.milestones = self._normalize_milestones(milestones)

        self._validate_sum()

    def _validate_sum(self) -> None:
        total = sum(self.phases.values()) + self.unattributed_ms
        if abs(self.total_ms - total) > ROUNDING_TOLERANCE_MS:
            raise StartupOperationTimingError(
                f"sum(phases)={total} diverges from totalMs={self.total_ms}"
            )

    def _normalize_milestones(self, milestones: dict[str, int] | None) -> dict[str, int]:
        normalized = {name: 0 for name in MILESTONES}
        for name, value in (milestones or {}).items():
            if name not in MILESTONES:
                raise StartupOperationTimingError(f"unknown milestone: {name}")
            value = int(value)
            if value < 0:
                raise StartupOperationTimingError(f"negative milestone: {name}={value}")
            if value > self.total_ms + ROUNDING_TOLERANCE_MS:
                raise StartupOperationTimingError(
                    f"milestone {name}={value} exceeds totalMs={self.total_ms}"
                )
            normalized[name] = value
        return normalized

    @classmethod
    def assemble(
        cls,
        *,
        strategy: str,
        total_ms: int,
        phase_reports: tuple[dict[str, int] | None, ...] | list[dict[str, int] | None],
        milestones: dict[str, int] | None = None,
    ) -> "StartupOperationTiming":
        """从各层局部报告合并生成最终计时结构。

        ``phase_reports`` 接受一个 dict 或 dict 的可迭代；单个 dict 会被视为
        一份报告，避免调用方少包一层元组时被误当成 key 迭代。
        """
        if isinstance(phase_reports, dict):
            reports = (phase_reports,)
        else:
            reports = tuple(phase_reports)
        return cls(
            strategy=strategy,
            total_ms=total_ms,
            phases=merge_phases(*reports),
            milestones=milestones,
        )

    def to_dict(self) -> dict:
        """输出权威 JSON 结构；九个 phase 全量出现，unattributedMs 单列。"""
        phases_out = dict(self.phases)
        phases_out["unattributedMs"] = self.unattributed_ms
        return {
            "startupOperationTiming": {
                "version": STARTUP_OPERATION_TIMING_VERSION,
                "strategy": self.strategy,
                "totalMs": self.total_ms,
                "phases": phases_out,
                "milestones": dict(self.milestones),
            }
        }
