#!/usr/bin/env python3
"""Linux Host 预热容量探针测试。"""

import tempfile
import unittest
from pathlib import Path

from prewarm_capacity import CapacityProbeError, LinuxHostCapacityProbe


class FakeNetworkManager:
    def __init__(self, free_slots: int) -> None:
        self.free_slots = free_slots

    def free_slot_count(self) -> int:
        return self.free_slots


class LinuxHostCapacityProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.meminfo = Path(self.temporary.name) / "meminfo"

    def probe(self, free_slots: int = 7) -> LinuxHostCapacityProbe:
        return LinuxHostCapacityProbe(
            FakeNetworkManager(free_slots),
            meminfo_path=self.meminfo,
        )

    def test_reads_available_memory_and_network_slots(self) -> None:
        self.meminfo.write_text(
            "MemTotal:       8192000 kB\nMemAvailable:   3145728 kB\n",
            encoding="utf-8",
        )
        capacity = self.probe().snapshot()
        self.assertEqual(capacity.available_mib, 3072)
        self.assertEqual(capacity.free_network_slots, 7)

    def test_missing_memavailable_is_hard_failure(self) -> None:
        self.meminfo.write_text("MemTotal: 8192000 kB\n", encoding="utf-8")
        with self.assertRaisesRegex(CapacityProbeError, "MemAvailable is missing"):
            self.probe().snapshot()

    def test_invalid_memavailable_is_hard_failure(self) -> None:
        self.meminfo.write_text("MemAvailable: unknown kB\n", encoding="utf-8")
        with self.assertRaisesRegex(CapacityProbeError, "invalid MemAvailable"):
            self.probe().snapshot()

    def test_negative_network_slots_are_rejected(self) -> None:
        self.meminfo.write_text("MemAvailable: 1048576 kB\n", encoding="utf-8")
        with self.assertRaisesRegex(CapacityProbeError, "negative slot"):
            self.probe(-1).snapshot()
