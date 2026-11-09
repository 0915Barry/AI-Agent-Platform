#!/usr/bin/env python3
"""生命周期显式停止与 watcher 协调的单元测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lifecycle


class ProcessStatTests(unittest.TestCase):
    @staticmethod
    def stat_line(command: str, state: str = "S", start_ticks: int = 987654) -> str:
        # state 是字段 3；下面依次生成字段 4 到 21，再追加字段 22 starttime。
        fields_before_starttime = [str(field) for field in range(4, 22)]
        return " ".join(
            [f"123 ({command})", state, *fields_before_starttime, str(start_ticks)]
        )

    def test_parse_process_stat_reads_state_and_starttime(self) -> None:
        self.assertEqual(
            lifecycle.parse_process_stat(self.stat_line("firecracker", "R", 123456)),
            ("R", 123456),
        )

    def test_parse_process_stat_allows_spaces_and_parentheses_in_comm(self) -> None:
        self.assertEqual(
            lifecycle.parse_process_stat(
                self.stat_line("worker name (generation 2)", "S", 7654321)
            ),
            ("S", 7654321),
        )

    def test_parse_process_stat_rejects_incomplete_content(self) -> None:
        with self.assertRaises(ValueError):
            lifecycle.parse_process_stat("123 (broken process) S 1 2 3")


class FakeRuntimeStore:
    def __init__(self, metadata: dict) -> None:
        self.metadata = dict(metadata)
        self.saved_statuses: list[str] = []

    def load_legacy(self, _legacy_path: Path, _instance_id: str) -> dict:
        return dict(self.metadata)

    def save(self, metadata: dict) -> dict:
        self.metadata = dict(metadata)
        self.saved_statuses.append(str(metadata["status"]))
        return dict(metadata)


class LifecycleStopTests(unittest.TestCase):
    def test_stop_uses_intent_marker_without_persisting_stopping_status(self) -> None:
        metadata = {
            "id": "agent-test",
            "pid": 123,
            "processStartTicks": 456,
            "status": "running",
            "jailPath": "/srv/jailer/firecracker/agent-test",
            "volumePath": "/srv/fc/volumes/users/user-test.ext4",
            "networkDatabasePath": "",
            "networkLeasePath": "",
            "tapName": "",
            "nftFamily": "inet",
            "nftTable": "",
            "sidecars": [],
        }
        store = FakeRuntimeStore(metadata)

        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary)
            marker = lifecycle.stop_intent_path(state_root, "agent-test")

            def observe_marker(_metadata: dict, *, guest_shutdown: bool = True) -> str:
                self.assertTrue(guest_shutdown)
                self.assertTrue(marker.is_file())
                return "sigterm"

            with (
                mock.patch.object(lifecycle, "stop_process", side_effect=observe_marker),
                mock.patch.object(lifecycle, "stop_sidecars"),
                mock.patch.object(lifecycle, "cleanup_ephemeral"),
            ):
                stopped = lifecycle.stop_instance(
                    store,
                    state_root,
                    "agent-test",
                    "api",
                )

        self.assertEqual(store.saved_statuses, ["stopped"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(marker.exists())


class WatcherDecisionTests(unittest.TestCase):
    @staticmethod
    def metadata(*, status: str = "running", pid: int = 123, ticks: int = 456) -> dict:
        return {
            "id": "agent-test",
            "pid": pid,
            "processStartTicks": ticks,
            "status": status,
            "createdAt": 100,
            "idleTimeoutSeconds": 300,
            "jailPath": "/srv/jailer/firecracker/agent-test",
            "apiSocket": "/srv/jailer/firecracker/agent-test/root/firecracker.socket",
            "volumePath": "/srv/fc/volumes/users/user-test.ext4",
            "networkDatabasePath": "",
            "networkLeasePath": "",
            "networkSubnet": "172.29.0.0/30",
            "hostIp": "172.29.0.1",
            "guestIp": "172.29.0.2",
            "rpcPort": 8080,
            "tapName": "fc00000000",
            "nftFamily": "inet",
            "nftTable": "aap_00000000",
            "sidecars": [],
        }

    def make_context(self, temporary: str, metadata: dict):
        state_root = Path(temporary)
        database = state_root / "control-plane.db"
        store = lifecycle.RuntimeStore(database)
        store.save(metadata)
        args = SimpleNamespace(
            database=database,
            state_root=state_root,
            instance_id="agent-test",
            poll_interval=0.01,
            owner_id="user-test",
        )
        return args, store

    def run_watcher(self, args, *, process_matches) -> tuple[mock.Mock, mock.Mock]:
        stop_sidecars = mock.Mock()
        cleanup_ephemeral = mock.Mock()
        with (
            mock.patch.object(lifecycle.os, "geteuid", return_value=0),
            mock.patch.object(lifecycle, "process_matches", side_effect=process_matches),
            mock.patch.object(lifecycle, "stop_sidecars", stop_sidecars),
            mock.patch.object(lifecycle, "cleanup_ephemeral", cleanup_ephemeral),
        ):
            lifecycle.command_watch(args)
        return stop_sidecars, cleanup_ephemeral

    def test_watcher_returns_when_instance_is_already_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args, store = self.make_context(
                temporary, self.metadata(status="stopped")
            )
            process_probe = mock.Mock(return_value=False)
            stop_sidecars, cleanup = self.run_watcher(
                args, process_matches=process_probe
            )

            self.assertEqual(store.get("agent-test")["status"], "stopped")
            process_probe.assert_not_called()
            stop_sidecars.assert_not_called()
            cleanup.assert_not_called()

    def test_watcher_reaps_crash_without_stop_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args, store = self.make_context(temporary, self.metadata())
            stop_sidecars, cleanup = self.run_watcher(
                args, process_matches=lambda _metadata: False
            )

            saved = store.get("agent-test")
            self.assertEqual(saved["status"], "stopped")
            self.assertEqual(saved["stopReason"], "process_exited")
            stop_sidecars.assert_called_once()
            cleanup.assert_called_once()

    def test_watcher_waits_while_stop_intent_is_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args, store = self.make_context(temporary, self.metadata())
            marker = lifecycle.stop_intent_path(args.state_root, "agent-test")
            marker.parent.mkdir(parents=True)
            marker.touch()

            def finish_stop(_seconds: float) -> None:
                stopped = store.get("agent-test")
                stopped["status"] = "stopped"
                store.save(stopped)

            with (
                mock.patch.object(lifecycle.os, "geteuid", return_value=0),
                mock.patch.object(lifecycle, "process_matches", return_value=False),
                mock.patch.object(lifecycle, "stop_sidecars") as stop_sidecars,
                mock.patch.object(lifecycle, "cleanup_ephemeral") as cleanup,
                mock.patch.object(lifecycle.time, "sleep", side_effect=finish_stop),
                mock.patch.object(lifecycle.time, "time", return_value=time.time()),
            ):
                lifecycle.command_watch(args)

            stop_sidecars.assert_not_called()
            cleanup.assert_not_called()

    def test_watcher_reaps_when_stop_intent_is_expired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args, store = self.make_context(temporary, self.metadata())
            marker = lifecycle.stop_intent_path(args.state_root, "agent-test")
            marker.parent.mkdir(parents=True)
            marker.touch()
            expired_at = time.time() - lifecycle.STOP_INTENT_TIMEOUT_SECONDS - 1
            marker.chmod(0o600)
            os.utime(marker, (expired_at, expired_at))

            with mock.patch.object(lifecycle.time, "time", return_value=time.time()):
                stop_sidecars, cleanup = self.run_watcher(
                    args, process_matches=lambda _metadata: False
                )

            self.assertEqual(store.get("agent-test")["stopReason"], "process_exited")
            stop_sidecars.assert_called_once()
            cleanup.assert_called_once()

    def test_old_watcher_returns_when_stop_completed_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args, store = self.make_context(temporary, self.metadata())

            def stop_before_reread(_metadata: dict) -> bool:
                stopped = store.get("agent-test")
                stopped["status"] = "stopped"
                store.save(stopped)
                return False

            stop_sidecars, cleanup = self.run_watcher(
                args, process_matches=stop_before_reread
            )

            self.assertEqual(store.get("agent-test")["status"], "stopped")
            stop_sidecars.assert_not_called()
            cleanup.assert_not_called()

    def test_old_watcher_returns_when_process_generation_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args, store = self.make_context(temporary, self.metadata())

            def replace_generation_before_reread(_metadata: dict) -> bool:
                replacement = store.get("agent-test")
                replacement["processStartTicks"] += 1
                store.save(replacement)
                return False

            stop_sidecars, cleanup = self.run_watcher(
                args, process_matches=replace_generation_before_reread
            )

            saved = store.get("agent-test")
            self.assertEqual(saved["status"], "running")
            self.assertEqual(saved["processStartTicks"], 457)
            stop_sidecars.assert_not_called()
            cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
