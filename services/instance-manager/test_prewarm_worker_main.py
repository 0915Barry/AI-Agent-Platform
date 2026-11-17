#!/usr/bin/env python3
"""预热 Worker 手动入口的依赖组装测试。"""

import argparse
import tempfile
import unittest
from pathlib import Path

from prewarm_capacity import HostCapacity
from prewarm_store import PrewarmStore
from prewarm_worker_main import build_parser, run


class FakeNetworkManager:
    pass


class FakeRuntime:
    instances: list["FakeRuntime"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.network_manager = FakeNetworkManager()
        self.prepared: list[tuple[str, str, int]] = []
        self.destroyed: list[str] = []
        self.__class__.instances.append(self)

    def prepare_prewarmer(
        self,
        runtime_id: str,
        claim_token: str,
        *,
        min_free_network_slots: int,
    ) -> dict:
        self.prepared.append((runtime_id, claim_token, min_free_network_slots))
        return {"pid": 4321, "processStartTicks": 8765}

    def destroy(self, instance_id: str, *, preserve_volume: bool = True) -> None:
        self.destroyed.append(instance_id)


class FakeCapacityProbe:
    def __init__(self, _network_manager) -> None:
        pass

    def snapshot(self) -> HostCapacity:
        return HostCapacity(available_mib=4096, free_network_slots=10)


class PrewarmWorkerMainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "control-plane.db"
        self.config = self.root / "prewarm.env"
        self.config.write_text(
            "PREWARM_ENABLED=1\n"
            "PREWARM_TARGET_READY=1\n"
            "PREWARM_MAX_TOTAL=1\n"
            "PREWARM_BUILD_CONCURRENCY=1\n"
            "PREWARM_TTL_SECONDS=300\n"
            "PREWARM_HEALTH_INTERVAL_SECONDS=2\n"
            "PREWARM_MIN_AVAILABLE_MIB=1024\n"
            "PREWARM_MIN_FREE_NETWORK_SLOTS=1\n",
            encoding="utf-8",
        )
        FakeRuntime.instances.clear()

    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            database=self.database,
            state_root=self.root,
            prewarm_config=self.config,
            profile_key="profile-test",
            max_backoff=30,
            once=True,
        )

    def test_parser_defaults_to_loop_mode(self) -> None:
        args = build_parser().parse_args([])
        self.assertFalse(args.once)
        self.assertEqual(args.profile_key, "default-2vcpu-1g")

    def test_once_mode_assembles_dependencies_and_creates_ready_item(self) -> None:
        messages: list[str] = []
        result = run(
            self.args(),
            runtime_factory=FakeRuntime,
            capacity_probe_factory=FakeCapacityProbe,
            emit=messages.append,
        )
        self.assertEqual(result, 0)
        self.assertEqual(len(FakeRuntime.instances), 1)
        runtime = FakeRuntime.instances[0]
        self.assertEqual(runtime.kwargs["state_root"], self.root)
        self.assertEqual(runtime.kwargs["state_database"], self.database)
        self.assertEqual(len(runtime.prepared), 1)
        counts = PrewarmStore(self.database).capacity_counts("profile-test")
        self.assertEqual(counts["claimable_ready"], 1)
        self.assertIn("PREWARM_WORKER_READY", messages[0])
        self.assertIn("status=ok", messages[1])

    def test_once_mode_is_noop_when_disabled(self) -> None:
        self.config.write_text("PREWARM_ENABLED=0\n", encoding="utf-8")
        result = run(
            self.args(),
            runtime_factory=FakeRuntime,
            capacity_probe_factory=FakeCapacityProbe,
            emit=lambda _message: None,
        )
        self.assertEqual(result, 0)
        self.assertEqual(FakeRuntime.instances[0].prepared, [])


if __name__ == "__main__":
    unittest.main()
