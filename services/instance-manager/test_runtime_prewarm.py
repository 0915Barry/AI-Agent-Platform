#!/usr/bin/env python3
"""InstanceRuntime 预热激活接口的 fake/mock 单元测试。"""

import subprocess
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from prewarm_activation import BoundReady
from runtime import InstanceRuntime, RuntimeFailure


class RuntimePrewarmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        jail_patcher = patch("runtime.FIRECRACKER_JAIL_ROOT", root / "jailer" / "firecracker")
        jail_patcher.start()
        self.addCleanup(jail_patcher.stop)
        self.runtime = InstanceRuntime(
            state_root=root / "state",
            volume_root=root / "volumes" / "users",
            state_database=root / "state" / "control-plane.db",
            snapshot_root=root / "snapshots",
        )
        self.runtime_id = "prewarm-1"
        self.logical_id = "instance-1"
        self.user_id = "user-1"
        self.operation_id = "operation-1"
        self.activation_id = "activation-1"
        self.volume = self.runtime.volume_root / f"{self.user_id}.ext4"
        self.volume.parent.mkdir(parents=True)
        self.volume.write_bytes(b"volume")

        self.paths = self.runtime.paths(self.runtime_id, self.runtime_id)
        self.paths["chroot"].mkdir(parents=True)
        self.paths["state"].mkdir(parents=True)
        (self.paths["chroot"] / "data.ext4").write_bytes(b"placeholder")
        (self.paths["state"] / "prewarm-token").write_text(
            "claim-token\n", encoding="utf-8"
        )
        self.paths["socket"].touch()

        self.metadata = {
            "id": self.runtime_id,
            "status": "running",
            "pid": 123,
            "processStartTicks": 456,
            "apiSocket": str(self.paths["socket"]),
            "sidecars": [],
            "volumePath": str(self.paths["state"] / "placeholder.ext4"),
        }
        self.network = {
            "instance_id": self.runtime_id,
            "status": "active",
            "host_ip": "172.29.0.1",
            "guest_ip": "172.29.0.2",
        }
        self.runtime.runtime_store = Mock()
        self.runtime.runtime_store.get.return_value = dict(self.metadata)
        self.runtime.runtime_store.resolve_runtime_id.side_effect = lambda value: value
        self.runtime.runtime_store.save.side_effect = lambda value: dict(value)
        self.runtime.network_manager = Mock()
        self.runtime.network_manager.store.get.return_value = dict(self.network)
        self.runtime.user_volume_store = Mock()
        self.runtime.ensure_host = Mock(return_value=(999, 999, 998, 998))
        self.runtime.create_volume = Mock()
        self.runtime.configure_agent_volume = Mock()
        self.runtime.start_model_gateway = Mock()
        self.runtime.firecracker_api = Mock()
        self.runtime.terminate_process = Mock()
        self.runtime.prewarm_activation = Mock()
        self.runtime.prewarm_activation.probe.return_value = "warm"
        self.runtime.prewarm_activation.activate.return_value = BoundReady(
            self.activation_id, "172.29.0.2", 8080
        )

    @staticmethod
    def fake_run(arguments: list[str], *, check: bool = True):
        if arguments[0] == "blkid":
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout="9580747b-07d7-457c-b70e-b14d4a9e8ce2\n",
                stderr="",
            )
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    def activate(self) -> dict:
        return self.runtime.activate_prewarmer(
            self.runtime_id,
            self.logical_id,
            self.user_id,
            str(self.volume),
            self.operation_id,
            self.activation_id,
            hashlib.sha256(b"claim-token").hexdigest(),
        )

    def prepare(self) -> dict:
        return self.runtime.prepare_prewarmer(
            self.runtime_id,
            "claim-token",
            min_free_network_slots=4,
        )

    @patch("runtime.threading.Thread")
    @patch("runtime.subprocess.Popen")
    @patch("runtime.os.chmod")
    @patch("runtime.os.chown")
    @patch("runtime.run", side_effect=fake_run.__func__)
    def test_prepare_prewarmer_reaches_warm_and_registers_runtime(
        self, _run, _chown, _chmod, popen, _thread
    ) -> None:
        process = Mock(pid=222)
        process.poll.return_value = None
        process.wait.return_value = 0
        popen.return_value = process
        self.runtime.runtime_store.get.return_value = None
        self.runtime.network_manager.free_slot_count.return_value = 5
        self.runtime.network_manager.allocate.return_value = {
            "instance_id": self.runtime_id,
            "status": "reserved",
            "host_ip": "172.29.0.1",
            "guest_ip": "172.29.0.2",
            "guest_cidr": "172.29.0.2/30",
            "guest_mac": "06:00:ac:1d:00:02",
            "tap": "fc00000000",
            "nft_table": "aap_00000000",
            "subnet": "172.29.0.0/30",
        }
        self.runtime.prepare_prewarm_jail = Mock()
        self.runtime.wait_for_socket = Mock(
            side_effect=lambda *_args: self.paths["console"].write_text(
                "WARM_READY activate_address=172.29.0.2:8081\n", encoding="utf-8"
            )
        )
        self.runtime.lifecycle = Mock()
        self.runtime.status = Mock(
            return_value={"id": self.runtime_id, "status": "running", "processAlive": True}
        )

        result = self.prepare()

        self.assertEqual(result["prewarmState"], "warm")
        self.runtime.network_manager.setup.assert_called_once()
        self.runtime.prewarm_activation.probe.assert_called_once_with(
            "172.29.0.2", 8081, "claim-token"
        )
        self.runtime.network_manager.activate.assert_called_once_with(self.runtime_id)
        register = self.runtime.lifecycle.call_args.args
        self.assertEqual(register[:2], ("register", self.runtime_id))
        self.assertIn(str(self.paths["prewarm_placeholder"]), register)
        self.assertEqual(
            self.paths["prewarm_token"].read_text(encoding="utf-8"), "claim-token\n"
        )

    def test_prepare_prewarmer_reserves_network_safety_margin(self) -> None:
        self.runtime.runtime_store.get.return_value = None
        self.runtime.network_manager.free_slot_count.return_value = 4
        with self.assertRaisesRegex(RuntimeFailure, "insufficient network slots"):
            self.prepare()
        self.runtime.network_manager.allocate.assert_not_called()

    def test_prepare_prewarmer_rejects_unsafe_token(self) -> None:
        with self.assertRaisesRegex(RuntimeFailure, "invalid prewarm claim token"):
            self.runtime.prepare_prewarmer(self.runtime_id, "bad token\n")
        self.runtime.ensure_host.assert_not_called()

    @patch("runtime.subprocess.Popen")
    @patch("runtime.os.chmod")
    @patch("runtime.os.chown")
    @patch("runtime.run", side_effect=fake_run.__func__)
    def test_prepare_probe_failure_cleans_process_network_and_secret(
        self, _run, _chown, _chmod, popen
    ) -> None:
        process = Mock(pid=222)
        process.poll.return_value = None
        popen.return_value = process
        self.runtime.runtime_store.get.return_value = None
        self.runtime.network_manager.free_slot_count.return_value = 5
        self.runtime.network_manager.allocate.return_value = {
            "instance_id": self.runtime_id,
            "status": "reserved",
            "host_ip": "172.29.0.1",
            "guest_ip": "172.29.0.2",
            "guest_cidr": "172.29.0.2/30",
            "guest_mac": "06:00:ac:1d:00:02",
            "tap": "fc00000000",
            "nft_table": "aap_00000000",
            "subnet": "172.29.0.0/30",
        }
        self.runtime.prepare_prewarm_jail = Mock()
        self.runtime.wait_for_socket = Mock(
            side_effect=lambda *_args: self.paths["console"].write_text(
                "WARM_READY activate_address=172.29.0.2:8081\n", encoding="utf-8"
            )
        )
        self.runtime.prewarm_activation.probe.return_value = "activated"

        with self.assertRaisesRegex(RuntimeFailure, "not in the warm state"):
            self.prepare()
        self.runtime.terminate_process.assert_called_once_with(process)
        self.runtime.network_manager.release.assert_called_once_with(self.runtime_id)
        self.assertFalse(self.paths["prewarm_token"].exists())

    @patch("runtime.process_start_ticks", return_value=789)
    @patch("runtime.process_matches", return_value=True)
    @patch("runtime.os.chmod")
    @patch("runtime.os.chown")
    @patch("runtime.Path.is_socket", return_value=True)
    @patch("runtime.run", side_effect=fake_run.__func__)
    def test_activate_swaps_drive_starts_gateway_and_saves_runtime(
        self, _run, _is_socket, _chown, _chmod, _matches, _ticks
    ) -> None:
        sidecar = SimpleNamespace(pid=321, wait=lambda: 0)
        self.runtime.start_model_gateway.return_value = [sidecar]

        result = self.activate()

        self.runtime.prewarm_activation.probe.assert_called_once_with(
            "172.29.0.2", 8081, "claim-token"
        )
        self.runtime.firecracker_api.assert_called_once_with(
            self.paths["socket"],
            "PATCH",
            "/drives/data",
            {"drive_id": "data", "path_on_host": "/data.ext4"},
        )
        self.runtime.prewarm_activation.activate.assert_called_once_with(
            "172.29.0.2",
            8081,
            "claim-token",
            self.activation_id,
            "9580747b-07d7-457c-b70e-b14d4a9e8ce2",
        )
        self.assertEqual(
            (self.paths["chroot"] / "data.ext4").stat().st_ino,
            self.volume.stat().st_ino,
        )
        saved = self.runtime.runtime_store.save.call_args.args[0]
        self.assertEqual(saved["volumePath"], str(self.volume))
        self.assertEqual(saved["sidecars"], [{"pid": 321, "processStartTicks": 789}])
        self.assertTrue(result["processAlive"])

    @patch("runtime.process_matches", return_value=False)
    @patch("runtime.Path.is_socket", return_value=True)
    def test_dead_or_reused_vmm_is_rejected_before_volume_changes(
        self, _is_socket, _matches
    ) -> None:
        with self.assertRaisesRegex(RuntimeFailure, "not alive"):
            self.activate()
        self.runtime.create_volume.assert_not_called()
        self.runtime.firecracker_api.assert_not_called()

    @patch("runtime.process_matches", return_value=True)
    @patch("runtime.os.chmod")
    @patch("runtime.os.chown")
    @patch("runtime.Path.is_socket", return_value=True)
    @patch("runtime.run", side_effect=fake_run.__func__)
    def test_unexpected_bound_endpoint_stops_new_gateway(
        self, _run, _is_socket, _chown, _chmod, _matches
    ) -> None:
        sidecar = SimpleNamespace(pid=321, wait=lambda: 0)
        self.runtime.start_model_gateway.return_value = [sidecar]
        self.runtime.prewarm_activation.activate.return_value = BoundReady(
            self.activation_id, "172.29.0.2", 9999
        )
        with self.assertRaisesRegex(RuntimeFailure, "unexpected Pi RPC endpoint"):
            self.activate()
        self.runtime.terminate_process.assert_called_once_with(sidecar)
        self.runtime.runtime_store.save.assert_not_called()

    @patch("runtime.process_matches", return_value=True)
    @patch("runtime.Path.is_socket", return_value=True)
    def test_activation_rejects_token_file_hash_mismatch(self, _is_socket, _matches) -> None:
        with self.assertRaisesRegex(RuntimeFailure, "token hash mismatch"):
            self.runtime.activate_prewarmer(
                self.runtime_id,
                self.logical_id,
                self.user_id,
                str(self.volume),
                self.operation_id,
                self.activation_id,
                "0" * 64,
            )
        self.runtime.prewarm_activation.probe.assert_not_called()

    @patch("runtime.process_matches", return_value=True)
    @patch("runtime.Path.is_socket", return_value=True)
    def test_commit_uses_same_live_generation_and_token(self, _is_socket, _matches) -> None:
        self.runtime.commit_prewarmer(
            self.runtime_id, self.operation_id, self.activation_id
        )
        self.runtime.prewarm_activation.commit.assert_called_once_with(
            "172.29.0.2", 8081, "claim-token", self.activation_id
        )

    @patch("runtime.process_matches", return_value=True)
    @patch("runtime.Path.is_socket", return_value=True)
    def test_commit_rejects_missing_token(self, _is_socket, _matches) -> None:
        (self.paths["state"] / "prewarm-token").unlink()
        with self.assertRaisesRegex(RuntimeFailure, "token is unavailable"):
            self.runtime.commit_prewarmer(
                self.runtime_id, self.operation_id, self.activation_id
            )


if __name__ == "__main__":
    unittest.main()
