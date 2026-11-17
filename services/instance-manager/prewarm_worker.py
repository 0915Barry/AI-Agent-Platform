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
from prewarm_claim_audit import PrewarmClaimAuditor
from prewarm_claim_compensation import (
    ActivationTimeoutClaimCompensator,
    CommitPendingClaimCompensator,
    PreActivationClaimCompensator,
)
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

    def stop(self, instance_id: str, reason: str = "api") -> dict: ...

    def status(self, instance_id: str) -> dict | None: ...


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


@dataclass(frozen=True)
class ReconcileResult:
    """Worker 重启时公共池核对结果。"""

    kept_runtime_ids: tuple[str, ...]
    retired_runtime_ids: tuple[str, ...]


@dataclass(frozen=True)
class HealthResult:
    """一次稳态 ready 进程健康检查结果。"""

    healthy_runtime_ids: tuple[str, ...]
    retired_runtime_ids: tuple[str, ...]


@dataclass(frozen=True)
class ClaimRecoveryResult:
    """一次超时领取巡检的处理结果。"""

    compensated_runtime_ids: tuple[str, ...]
    deferred_classifications: tuple[str, ...]
    errors: tuple[str, ...]


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
        claim_auditor: PrewarmClaimAuditor | None = None,
        claim_compensator: PreActivationClaimCompensator | None = None,
        activation_compensator: ActivationTimeoutClaimCompensator | None = None,
        commit_pending_compensator: CommitPendingClaimCompensator | None = None,
    ) -> None:
        self.config = config
        self.prewarm_store = prewarm_store
        self.profile_key = profile_key
        self.builder = builder
        self.capacity_probe = capacity_probe
        self.claim_auditor = claim_auditor
        self.claim_compensator = claim_compensator
        self.activation_compensator = activation_compensator
        self.commit_pending_compensator = commit_pending_compensator
        self.host_id = host_id or socket.gethostname()
        self._new_runtime_id = new_runtime_id or (
            lambda: f"prewarm-{uuid.uuid4().hex[:20]}"
        )
        self._new_claim_token = new_claim_token or (
            lambda: secrets.token_urlsafe(32)
        )

    def recover_stale_claims(self) -> ClaimRecoveryResult:
        """审计超时领取，并按分类分发给对应补偿器。

        ``pre_attach_timeout`` / ``reserved_timeout`` 由 PreActivationClaimCompensator
        回滚；``activation_timeout`` 由 ActivationTimeoutClaimCompensator 完整回滚；
        ``commit_pending`` 由 CommitPendingClaimCompensator 向前修复。其余异常分类
        （operation_missing / commit_incomplete / inconsistent 等）只报告不自动补偿。
        单项补偿失败只记录错误，使公共池的 TTL、健康检查和补池仍可继续运行；
        失败项保持隔离状态，等待下一轮或人工检查。
        """
        if self.claim_auditor is None:
            return ClaimRecoveryResult((), (), ())

        compensated: list[str] = []
        deferred: list[str] = []
        errors: list[str] = []
        for audit in self.claim_auditor.audit(self.config.claim_timeout_seconds):
            if audit.classification in ("pre_attach_timeout", "reserved_timeout"):
                compensator = self.claim_compensator
            elif audit.classification == "activation_timeout":
                compensator = self.activation_compensator
            elif audit.classification == "commit_pending":
                compensator = self.commit_pending_compensator
            else:
                compensator = None

            if compensator is None:
                deferred.append(f"{audit.runtime_id}:{audit.classification}")
                continue
            try:
                result = compensator.compensate(audit)
                compensated.append(result.runtime_id)
            except Exception as error:
                message = str(error).replace("\n", " ")[-1000:]
                errors.append(f"{audit.runtime_id}:{audit.classification}:{message}")
        return ClaimRecoveryResult(
            compensated_runtime_ids=tuple(compensated),
            deferred_classifications=tuple(deferred),
            errors=tuple(errors),
        )

    def update_config(self, config: PrewarmConfig) -> bool:
        """替换已经完整校验的新配置，返回配置是否发生变化。"""
        if config == self.config:
            return False
        self.config = config
        return True

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
        if not self.config.enabled:
            return self.fill_once(available_mib=0, free_network_slots=0)
        if self.capacity_probe is None:
            raise PrewarmWorkerError("prewarm capacity probe is not configured")
        capacity = self.capacity_probe.snapshot()
        return self.fill_once(
            available_mib=capacity.available_mib,
            free_network_slots=capacity.free_network_slots,
        )

    def reap_expired_ready(self) -> tuple[str, ...]:
        """回收本 profile 已过期的 ``ready`` 公共预热机。

        查询结果只是候选集合；真正操作前必须通过 ``mark_draining`` 再次匹配
        ``status=ready``。如果并发领取已经将其改为 ``claiming``，转换失败后会
        跳过该项，绝不会停止用户正在接管的 Runtime。

        停止或销毁失败时记录保持 ``draining``，不重新发布到公共池，等待后续
        reconcile 处理。
        """
        if self.builder is None:
            raise PrewarmWorkerError("prewarm builder is not configured")

        retired: list[str] = []
        for item in self.prewarm_store.list_expired_ready(self.profile_key):
            runtime_id = str(item["runtime_id"])
            if not self.prewarm_store.mark_draining(runtime_id):
                continue
            try:
                stopped = self.builder.stop(runtime_id, reason="prewarm_ttl_expired")
                if stopped.get("status") != "stopped":
                    raise PrewarmWorkerError(
                        f"expired prewarm runtime did not stop: {runtime_id}"
                    )
                self.builder.destroy(runtime_id, preserve_volume=True)
                if not self.prewarm_store.retire(runtime_id):
                    raise PrewarmWorkerError(
                        f"expired prewarm item did not retire: {runtime_id}"
                    )
            except Exception as error:
                if isinstance(error, PrewarmWorkerError):
                    raise
                raise PrewarmWorkerError(str(error)[-4000:]) from error
            retired.append(runtime_id)
        return tuple(retired)

    def reconcile_public(self) -> ReconcileResult:
        """核对 Worker 所属公共池，清理未完成或已经失联的项目。

        - 健康且代次一致的 ``ready`` 保留；
        - 重启遗留的 ``preparing/error/draining`` 清理并退休；
        - 元数据缺失或进程已死亡的 ``ready`` 清理并退休；
        - 活进程的 PID/启动代次与 SQLite 不一致时只隔离为 ``draining`` 并硬失败，
          绝不停止无法确认归属的进程。

        ``claiming/assigned`` 不在本方法查询范围内。
        """
        if self.builder is None:
            raise PrewarmWorkerError("prewarm builder is not configured")

        kept: list[str] = []
        retired: list[str] = []
        for item in self.prewarm_store.list_public_for_reconcile(self.profile_key):
            runtime_id = str(item["runtime_id"])
            item_status = str(item["status"])
            runtime_status = self.builder.status(runtime_id)

            if item_status == "ready" and self._runtime_generation_matches(
                item,
                runtime_status,
            ):
                kept.append(runtime_id)
                continue

            if (
                runtime_status is not None
                and runtime_status.get("processAlive") is True
                and not self._runtime_generation_matches(item, runtime_status)
            ):
                if item_status in {"ready", "error"}:
                    self.prewarm_store.mark_draining(runtime_id)
                elif item_status == "preparing":
                    self.prewarm_store.mark_prepare_error(
                        runtime_id,
                        "runtime generation mismatch during reconcile",
                    )
                    self.prewarm_store.mark_draining(runtime_id)
                raise PrewarmWorkerError(
                    f"refusing to stop mismatched prewarm runtime generation: {runtime_id}"
                )

            if item_status == "preparing":
                self.prewarm_store.mark_prepare_error(
                    runtime_id,
                    "worker restarted before prewarm item became ready",
                )
                item_status = "error"
            if item_status in {"ready", "error"}:
                if not self.prewarm_store.mark_draining(runtime_id):
                    continue

            runtime_status = self.builder.status(runtime_id)
            if runtime_status is not None and runtime_status.get("processAlive") is True:
                stopped = self.builder.stop(runtime_id, reason="prewarm_reconcile")
                if stopped.get("status") != "stopped":
                    raise PrewarmWorkerError(
                        f"reconciled prewarm runtime did not stop: {runtime_id}"
                    )
            self.builder.destroy(runtime_id, preserve_volume=True)
            if not self.prewarm_store.retire(runtime_id):
                raise PrewarmWorkerError(
                    f"reconciled prewarm item did not retire: {runtime_id}"
                )
            retired.append(runtime_id)

        return ReconcileResult(
            kept_runtime_ids=tuple(kept),
            retired_runtime_ids=tuple(retired),
        )

    def check_ready_health(self) -> HealthResult:
        """核对当前仍可领取的 ``ready`` Firecracker 进程代次。

        TTL 回收由 ``reap_expired_ready`` 先执行，因此这里仅检查尚未过期的项目。
        进程死亡或元数据消失时安全清理并退休；活进程的代次不一致时只转为
        ``draining`` 并硬失败，防止 PID 复用导致误杀其他进程。
        """
        if self.builder is None:
            raise PrewarmWorkerError("prewarm builder is not configured")

        healthy: list[str] = []
        retired: list[str] = []
        for item in self.prewarm_store.list_ready(self.profile_key):
            runtime_id = str(item["runtime_id"])
            runtime_status = self.builder.status(runtime_id)
            if self._runtime_generation_matches(item, runtime_status):
                healthy.append(runtime_id)
                continue

            if runtime_status is not None and runtime_status.get("processAlive") is True:
                self.prewarm_store.mark_draining(runtime_id)
                raise PrewarmWorkerError(
                    f"refusing to stop mismatched prewarm runtime generation: {runtime_id}"
                )

            if not self.prewarm_store.mark_draining(runtime_id):
                continue
            self.builder.destroy(runtime_id, preserve_volume=True)
            if not self.prewarm_store.retire(runtime_id):
                raise PrewarmWorkerError(
                    f"unhealthy prewarm item did not retire: {runtime_id}"
                )
            retired.append(runtime_id)

        return HealthResult(
            healthy_runtime_ids=tuple(healthy),
            retired_runtime_ids=tuple(retired),
        )

    def drain_excess_ready(self) -> tuple[str, ...]:
        """按当前目标回收多余公共 ``ready``；关闭时目标视为零。

        ``list_ready`` 只返回仍可领取的公共项，天然排除 ``claiming/assigned``。
        回收前再次执行 ``ready → draining`` 条件转换，若并发领取已经发生则跳过，
        不会停止用户实例。
        """
        if self.builder is None:
            raise PrewarmWorkerError("prewarm builder is not configured")
        target = self.config.target_ready if self.config.enabled else 0
        ready_items = self.prewarm_store.list_ready(self.profile_key)
        excess = max(0, len(ready_items) - target)
        retired: list[str] = []
        for item in ready_items[:excess]:
            runtime_id = str(item["runtime_id"])
            if not self.prewarm_store.mark_draining(runtime_id):
                continue
            try:
                runtime_status = self.builder.status(runtime_id)
                if runtime_status is not None and runtime_status.get("processAlive") is True:
                    if not self._runtime_generation_matches(item, runtime_status):
                        raise PrewarmWorkerError(
                            f"refusing to stop mismatched prewarm runtime generation: {runtime_id}"
                        )
                    stopped = self.builder.stop(runtime_id, reason="prewarm_pool_resize")
                    if stopped.get("status") != "stopped":
                        raise PrewarmWorkerError(
                            f"excess prewarm runtime did not stop: {runtime_id}"
                        )
                self.builder.destroy(runtime_id, preserve_volume=True)
                if not self.prewarm_store.retire(runtime_id):
                    raise PrewarmWorkerError(
                        f"excess prewarm item did not retire: {runtime_id}"
                    )
            except Exception as error:
                if isinstance(error, PrewarmWorkerError):
                    raise
                raise PrewarmWorkerError(str(error)[-4000:]) from error
            retired.append(runtime_id)
        return tuple(retired)

    @staticmethod
    def _runtime_generation_matches(item: dict, runtime_status: dict | None) -> bool:
        """ready 项只有在进程存活且 PID/start ticks 完全一致时才可继续使用。"""
        if runtime_status is None or runtime_status.get("processAlive") is not True:
            return False
        expected_pid = item.get("vmm_pid")
        expected_ticks = item.get("process_start_ticks")
        if expected_pid is None or expected_ticks is None:
            return False
        return (
            int(runtime_status.get("pid", -1)) == int(expected_pid)
            and int(runtime_status.get("processStartTicks", -1)) == int(expected_ticks)
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
