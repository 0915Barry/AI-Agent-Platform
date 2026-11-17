"""预热 Worker 使用的 Linux Host 容量探针。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class CapacityProbeError(RuntimeError):
    """Host 容量信息不可读，Worker 必须暂停补池。"""


class NetworkCapacity(Protocol):
    def free_slot_count(self) -> int: ...


@dataclass(frozen=True)
class HostCapacity:
    available_mib: int
    free_network_slots: int


class LinuxHostCapacityProbe:
    """读取 Linux 可用内存和 NetworkManager 的剩余网络槽位。"""

    def __init__(
        self,
        network_manager: NetworkCapacity,
        *,
        meminfo_path: Path = Path("/proc/meminfo"),
    ) -> None:
        self.network_manager = network_manager
        self.meminfo_path = meminfo_path

    def snapshot(self) -> HostCapacity:
        available_mib = self._available_mib()
        free_network_slots = int(self.network_manager.free_slot_count())
        if free_network_slots < 0:
            raise CapacityProbeError("network manager returned a negative slot count")
        return HostCapacity(
            available_mib=available_mib,
            free_network_slots=free_network_slots,
        )

    def _available_mib(self) -> int:
        try:
            lines = self.meminfo_path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise CapacityProbeError("cannot read Linux memory information") from error
        for line in lines:
            key, separator, raw_value = line.partition(":")
            if key != "MemAvailable" or not separator:
                continue
            fields = raw_value.split()
            if not fields:
                break
            try:
                available_kib = int(fields[0])
            except ValueError as error:
                raise CapacityProbeError("invalid MemAvailable value") from error
            if available_kib < 0:
                raise CapacityProbeError("invalid MemAvailable value")
            return available_kib // 1024
        raise CapacityProbeError("MemAvailable is missing from Linux memory information")
