"""预热 Worker 的可停止后台巡检循环与失败退避。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from prewarm_worker import FillResult, HealthResult, ReconcileResult


class FillTarget(Protocol):
    def reconcile_public(self) -> ReconcileResult: ...

    def reap_expired_ready(self) -> tuple[str, ...]: ...

    def check_ready_health(self) -> HealthResult: ...

    def fill_once_from_host(self) -> FillResult: ...


class StopSignal(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> bool: ...


@dataclass(frozen=True)
class WorkerCycle:
    success: bool
    next_delay_seconds: int
    created_runtime_ids: tuple[str, ...] = ()
    retired_runtime_ids: tuple[str, ...] = ()
    reason: str = ""
    error: str | None = None


class PrewarmWorkerLoop:
    """立即巡检一次，之后按间隔运行；失败时指数退避。"""

    def __init__(
        self,
        worker: FillTarget,
        *,
        interval_seconds: int,
        max_backoff_seconds: int = 60,
        emit: Callable[[str], None] | None = None,
    ) -> None:
        if interval_seconds < 1:
            raise ValueError("worker interval must be positive")
        if max_backoff_seconds < interval_seconds:
            raise ValueError("worker max backoff must be >= interval")
        self.worker = worker
        self.interval_seconds = interval_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.emit = emit or (lambda message: print(message, flush=True))
        self.consecutive_failures = 0

    def reconcile_once(self) -> WorkerCycle:
        """执行启动协调；失败时返回退避信息且不进入正常补池。"""
        try:
            result = self.worker.reconcile_public()
        except Exception as error:
            return self._error_cycle("reconcile", error)

        self.consecutive_failures = 0
        cycle = WorkerCycle(
            success=True,
            next_delay_seconds=0,
            retired_runtime_ids=result.retired_runtime_ids,
            reason="reconciled",
        )
        self.emit(
            "PREWARM_WORKER_RECONCILE status=ok "
            f"kept={len(result.kept_runtime_ids)} "
            f"retired={len(result.retired_runtime_ids)}"
        )
        return cycle

    def run_once(self) -> WorkerCycle:
        """执行一轮并返回下次等待时间；异常不穿透后台主循环。"""
        try:
            expired_runtime_ids = self.worker.reap_expired_ready()
            health = self.worker.check_ready_health()
            retired_runtime_ids = (
                *expired_runtime_ids,
                *health.retired_runtime_ids,
            )
            result = self.worker.fill_once_from_host()
        except Exception as error:
            return self._error_cycle("cycle", error)

        self.consecutive_failures = 0
        cycle = WorkerCycle(
            success=True,
            next_delay_seconds=self.interval_seconds,
            created_runtime_ids=result.created_runtime_ids,
            retired_runtime_ids=retired_runtime_ids,
            reason=result.plan.reason,
        )
        self.emit(
            "PREWARM_WORKER_CYCLE status=ok "
            f"reason={result.plan.reason} planned={result.plan.create_count} "
            f"retired={len(retired_runtime_ids)} created={len(result.created_runtime_ids)} "
            f"next_delay={self.interval_seconds}s"
        )
        return cycle

    def _error_cycle(self, stage: str, error: Exception) -> WorkerCycle:
        self.consecutive_failures += 1
        exponent = min(self.consecutive_failures - 1, 20)
        delay = min(
            self.max_backoff_seconds,
            self.interval_seconds * (2**exponent),
        )
        message = str(error).replace("\n", " ")[-1000:]
        cycle = WorkerCycle(
            success=False,
            next_delay_seconds=delay,
            reason=f"{stage}_error",
            error=message,
        )
        self.emit(
            f"PREWARM_WORKER_{stage.upper()} status=error "
            f"failures={self.consecutive_failures} next_delay={delay}s error={message}"
        )
        return cycle

    def run(self, stop_signal: StopSignal) -> None:
        """运行到停止信号；使用 ``wait`` 使停止不必等完整睡眠周期。"""
        while not stop_signal.is_set():
            reconciliation = self.reconcile_once()
            if reconciliation.success:
                break
            if stop_signal.wait(reconciliation.next_delay_seconds):
                return
        while not stop_signal.is_set():
            cycle = self.run_once()
            if stop_signal.wait(cycle.next_delay_seconds):
                break
