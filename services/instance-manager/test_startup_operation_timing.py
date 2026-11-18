#!/usr/bin/env python3
"""统一操作计时数据结构的纯单元测试。"""

import unittest

from startup_operation_timing import (
    KNOWN_STRATEGIES,
    MILESTONES,
    PHASES,
    ROUNDING_TOLERANCE_MS,
    STARTUP_OPERATION_TIMING_VERSION,
    StartupOperationTiming,
    StartupOperationTimingError,
    compute_unattributed,
    map_cold_host_stages,
    merge_phases,
)


class MergePhasesTests(unittest.TestCase):
    def test_disjoint_reports_are_merged(self) -> None:
        merged = merge_phases(
            {"decisionMs": 2, "claimMs": 10},
            {"volumePrepareMs": 80, "commitMs": 20},
        )
        self.assertEqual(
            merged,
            {"decisionMs": 2, "claimMs": 10, "volumePrepareMs": 80, "commitMs": 20},
        )

    def test_none_and_empty_reports_are_ignored(self) -> None:
        self.assertEqual(merge_phases(None, {}, {"decisionMs": 1}), {"decisionMs": 1})

    def test_duplicate_phase_is_rejected(self) -> None:
        with self.assertRaisesRegex(StartupOperationTimingError, "duplicate phase"):
            merge_phases({"decisionMs": 1}, {"decisionMs": 2})

    def test_unknown_phase_is_rejected(self) -> None:
        with self.assertRaisesRegex(StartupOperationTimingError, "unknown phase"):
            merge_phases({"bogusMs": 1})

    def test_negative_phase_is_rejected(self) -> None:
        with self.assertRaisesRegex(StartupOperationTimingError, "negative phase"):
            merge_phases({"decisionMs": -1})


class MapColdHostStagesTests(unittest.TestCase):
    def test_maps_cold_stages_to_phases(self) -> None:
        phases = map_cold_host_stages(
            {
                "ensure_host_ms": 5,
                "volume_create_ms": 10,
                "e2fsck_ms": 20,
                "network_allocate_ms": 30,
                "volume_config_ms": 40,
                "network_setup_ms": 50,
                "gateway_ready_ms": 60,
                "jail_prepare_ms": 70,
                "guest_ready_ms": 80,
                "lifecycle_ms": 90,
            }
        )
        self.assertEqual(
            phases,
            {
                "volumePrepareMs": 70,
                "networkReadyMs": 80,
                "gatewayReadyMs": 60,
                "runtimeReadyMs": 240,
            },
        )

    def test_ensure_host_is_not_mapped(self) -> None:
        phases = map_cold_host_stages({"ensure_host_ms": 5000})
        self.assertEqual(
            phases,
            {
                "volumePrepareMs": 0,
                "networkReadyMs": 0,
                "gatewayReadyMs": 0,
                "runtimeReadyMs": 0,
            },
        )

    def test_unknown_stage_is_ignored(self) -> None:
        phases = map_cold_host_stages({"future_stage_ms": 999, "gateway_ready_ms": 7})
        self.assertEqual(phases["gatewayReadyMs"], 7)
        self.assertEqual(phases["volumePrepareMs"], 0)

    def test_none_and_empty_default_to_zero(self) -> None:
        for report in (None, {}):
            self.assertEqual(
                map_cold_host_stages(report),
                {
                    "volumePrepareMs": 0,
                    "networkReadyMs": 0,
                    "gatewayReadyMs": 0,
                    "runtimeReadyMs": 0,
                },
            )


class ComputeUnattributedTests(unittest.TestCase):
    def test_remainder_is_total_minus_known(self) -> None:
        self.assertEqual(compute_unattributed(100, {"decisionMs": 10}), 90)

    def test_remainder_floor_is_zero(self) -> None:
        self.assertEqual(compute_unattributed(100, {"decisionMs": 150}), 0)

    def test_missing_phases_count_as_zero(self) -> None:
        self.assertEqual(compute_unattributed(100, {}), 100)


class AssembleTests(unittest.TestCase):
    def test_assembles_and_computes_unattributed(self) -> None:
        timing = StartupOperationTiming.assemble(
            strategy="prewarm",
            total_ms=662,
            phase_reports=(
                {"decisionMs": 2, "claimMs": 10},
                {"volumePrepareMs": 80, "gatewayReadyMs": 200, "drivePatchMs": 50},
                {"guestActivateMs": 300, "commitMs": 20},
            ),
        )
        self.assertEqual(timing.strategy, "prewarm")
        self.assertEqual(timing.total_ms, 662)
        self.assertEqual(timing.unattributed_ms, 0)
        # 不适用 phase 固定为 0，而不是省略。
        self.assertEqual(timing.phases["networkReadyMs"], 0)
        self.assertEqual(timing.phases["runtimeReadyMs"], 0)

    def test_missing_phases_default_to_zero(self) -> None:
        timing = StartupOperationTiming.assemble(
            strategy="cold",
            total_ms=5100,
            phase_reports=({"decisionMs": 2, "runtimeReadyMs": 4800, "commitMs": 20}),
        )
        # 余数承接未上报的 278ms。
        self.assertEqual(timing.unattributed_ms, 278)
        self.assertEqual(
            timing.phases,
            {
                "decisionMs": 2,
                "claimMs": 0,
                "volumePrepareMs": 0,
                "networkReadyMs": 0,
                "gatewayReadyMs": 0,
                "runtimeReadyMs": 4800,
                "drivePatchMs": 0,
                "guestActivateMs": 0,
                "commitMs": 20,
            },
        )

    def test_sum_divergence_beyond_tolerance_is_rejected(self) -> None:
        with self.assertRaisesRegex(StartupOperationTimingError, "diverges"):
            StartupOperationTiming.assemble(
                strategy="cold",
                total_ms=100,
                phase_reports=({"decisionMs": 1000}),
            )

    def test_sum_within_tolerance_is_allowed(self) -> None:
        timing = StartupOperationTiming.assemble(
            strategy="cold",
            total_ms=100,
            phase_reports=(
                {"decisionMs": 100 + ROUNDING_TOLERANCE_MS - 1},
            ),
        )
        # 已知 phase 略超 totalMs，余数被钳到 0，仍在容差内。
        self.assertEqual(timing.unattributed_ms, 0)

    def test_unknown_strategy_is_rejected(self) -> None:
        with self.assertRaisesRegex(StartupOperationTimingError, "unknown strategy"):
            StartupOperationTiming.assemble(
                strategy="warm",
                total_ms=10,
                phase_reports=(),
            )

    def test_negative_total_is_rejected(self) -> None:
        with self.assertRaisesRegex(StartupOperationTimingError, "negative totalMs"):
            StartupOperationTiming.assemble(
                strategy="cold", total_ms=-1, phase_reports=()
            )

    def test_unknown_phase_in_direct_init_is_rejected(self) -> None:
        with self.assertRaisesRegex(StartupOperationTimingError, "unknown phase"):
            StartupOperationTiming(
                strategy="cold", total_ms=10, phases={"bogusMs": 1}
            )


class MilestoneTests(unittest.TestCase):
    def test_milestone_is_normalized_and_defaults_to_zero(self) -> None:
        timing = StartupOperationTiming.assemble(
            strategy="cold",
            total_ms=5100,
            phase_reports=(),
            milestones={"guestDeclaredRpcReadyMs": 4970},
        )
        self.assertEqual(timing.milestones["guestDeclaredRpcReadyMs"], 4970)

    def test_unknown_milestone_is_rejected(self) -> None:
        with self.assertRaisesRegex(StartupOperationTimingError, "unknown milestone"):
            StartupOperationTiming.assemble(
                strategy="cold",
                total_ms=10,
                phase_reports=(),
                milestones={"firstRpcResponseMs": 100},
            )

    def test_negative_milestone_is_rejected(self) -> None:
        with self.assertRaisesRegex(StartupOperationTimingError, "negative milestone"):
            StartupOperationTiming.assemble(
                strategy="cold",
                total_ms=10,
                phase_reports=(),
                milestones={"guestDeclaredRpcReadyMs": -1},
            )

    def test_milestone_beyond_total_is_rejected(self) -> None:
        with self.assertRaisesRegex(StartupOperationTimingError, "exceeds totalMs"):
            StartupOperationTiming.assemble(
                strategy="cold",
                total_ms=10,
                phase_reports=(),
                milestones={"guestDeclaredRpcReadyMs": 100},
            )


class ToDictTests(unittest.TestCase):
    def test_shape_contains_all_fields(self) -> None:
        timing = StartupOperationTiming.assemble(
            strategy="prewarm",
            total_ms=662,
            phase_reports=({"decisionMs": 2}),
            milestones={"guestDeclaredRpcReadyMs": 600},
        )
        payload = timing.to_dict()["startupOperationTiming"]
        self.assertEqual(payload["version"], STARTUP_OPERATION_TIMING_VERSION)
        self.assertEqual(payload["strategy"], "prewarm")
        self.assertEqual(payload["totalMs"], 662)
        self.assertEqual(
            set(payload["phases"]),
            set(PHASES) | {"unattributedMs"},
        )
        self.assertEqual(set(payload["milestones"]), set(MILESTONES))


if __name__ == "__main__":
    unittest.main()
