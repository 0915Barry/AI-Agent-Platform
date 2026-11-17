#!/usr/bin/env python3
"""预热 Worker 后台循环与退避测试。"""

import unittest

from prewarm_worker import FillPlan, FillResult
from prewarm_worker_loop import PrewarmWorkerLoop


class FakeWorker:
    def __init__(self, outcomes, *, retired=(), reap_error=None) -> None:
        self.outcomes = list(outcomes)
        self.retired = tuple(retired)
        self.reap_error = reap_error
        self.calls = 0
        self.reap_calls = 0

    def reap_expired_ready(self) -> tuple[str, ...]:
        self.reap_calls += 1
        if self.reap_error is not None:
            raise self.reap_error
        return self.retired

    def fill_once_from_host(self) -> FillResult:
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeStopSignal:
    def __init__(self, stop_after_waits: int) -> None:
        self.stop_after_waits = stop_after_waits
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return len(self.waits) >= self.stop_after_waits

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return self.is_set()


def fill_result(*runtime_ids: str, reason: str = "fill") -> FillResult:
    plan = FillPlan(
        create_count=len(runtime_ids),
        reason=reason,
        claimable_ready=0,
        preparing=0,
        claiming=0,
        total_public=0,
    )
    return FillResult(plan=plan, created_runtime_ids=tuple(runtime_ids))


class PrewarmWorkerLoopTests(unittest.TestCase):
    def test_success_uses_normal_interval_and_emits_summary(self) -> None:
        messages: list[str] = []
        loop = PrewarmWorkerLoop(
            FakeWorker([fill_result("runtime-1")]),
            interval_seconds=5,
            emit=messages.append,
        )
        cycle = loop.run_once()
        self.assertTrue(cycle.success)
        self.assertEqual(cycle.next_delay_seconds, 5)
        self.assertEqual(cycle.created_runtime_ids, ("runtime-1",))
        self.assertIn("status=ok", messages[0])
        self.assertIn("created=1", messages[0])

    def test_cycle_reaps_expired_items_before_filling(self) -> None:
        messages: list[str] = []
        worker = FakeWorker(
            [fill_result("runtime-new")],
            retired=("runtime-old",),
        )
        loop = PrewarmWorkerLoop(worker, interval_seconds=5, emit=messages.append)
        cycle = loop.run_once()
        self.assertTrue(cycle.success)
        self.assertEqual(cycle.retired_runtime_ids, ("runtime-old",))
        self.assertEqual(cycle.created_runtime_ids, ("runtime-new",))
        self.assertEqual(worker.reap_calls, 1)
        self.assertEqual(worker.calls, 1)
        self.assertIn("retired=1", messages[0])

    def test_reap_failure_prevents_fill_and_enters_backoff(self) -> None:
        worker = FakeWorker(
            [fill_result("must-not-start")],
            reap_error=RuntimeError("retire failed"),
        )
        loop = PrewarmWorkerLoop(
            worker,
            interval_seconds=5,
            emit=lambda _message: None,
        )
        cycle = loop.run_once()
        self.assertFalse(cycle.success)
        self.assertEqual(cycle.next_delay_seconds, 5)
        self.assertEqual(worker.calls, 0)

    def test_failures_back_off_exponentially_and_cap(self) -> None:
        worker = FakeWorker([RuntimeError("host unavailable")])
        loop = PrewarmWorkerLoop(
            worker,
            interval_seconds=5,
            max_backoff_seconds=20,
            emit=lambda _message: None,
        )
        delays = [loop.run_once().next_delay_seconds for _ in range(5)]
        self.assertEqual(delays, [5, 10, 20, 20, 20])
        self.assertEqual(loop.consecutive_failures, 5)

    def test_success_resets_failure_backoff(self) -> None:
        worker = FakeWorker(
            [RuntimeError("first"), RuntimeError("second"), fill_result(reason="target_satisfied")]
        )
        loop = PrewarmWorkerLoop(
            worker,
            interval_seconds=5,
            max_backoff_seconds=60,
            emit=lambda _message: None,
        )
        self.assertEqual(loop.run_once().next_delay_seconds, 5)
        self.assertEqual(loop.run_once().next_delay_seconds, 10)
        success = loop.run_once()
        self.assertTrue(success.success)
        self.assertEqual(success.next_delay_seconds, 5)
        self.assertEqual(loop.consecutive_failures, 0)

    def test_error_log_is_single_line_and_bounded(self) -> None:
        messages: list[str] = []
        loop = PrewarmWorkerLoop(
            FakeWorker([RuntimeError("first line\n" + "x" * 1200)]),
            interval_seconds=5,
            emit=messages.append,
        )
        cycle = loop.run_once()
        self.assertFalse(cycle.success)
        self.assertNotIn("\n", messages[0])
        self.assertLess(len(cycle.error or ""), 1001)

    def test_run_stops_via_interruptible_wait(self) -> None:
        worker = FakeWorker([fill_result(reason="target_satisfied")])
        signal = FakeStopSignal(stop_after_waits=2)
        loop = PrewarmWorkerLoop(
            worker,
            interval_seconds=7,
            emit=lambda _message: None,
        )
        loop.run(signal)
        self.assertEqual(worker.calls, 2)
        self.assertEqual(signal.waits, [7, 7])

    def test_rejects_invalid_intervals(self) -> None:
        worker = FakeWorker([fill_result()])
        with self.assertRaises(ValueError):
            PrewarmWorkerLoop(worker, interval_seconds=0)
        with self.assertRaises(ValueError):
            PrewarmWorkerLoop(worker, interval_seconds=10, max_backoff_seconds=5)


if __name__ == "__main__":
    unittest.main()
