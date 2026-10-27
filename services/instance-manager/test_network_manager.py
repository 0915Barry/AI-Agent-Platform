#!/usr/bin/env python3
"""NetworkManager 的纯本地单元测试，不创建真实 TAP 或 nftables 规则。"""

import json
import io
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

import lab_network
from network_manager import FileLeaseStore, LinuxNetworkDriver, NetworkManager


class FakeRunner:
    """用内存集合模拟测试涉及的 ip 与 nft 子命令。"""

    def __init__(self) -> None:
        self.routes: set[str] = set()
        self.links: set[str] = set()
        self.tables: set[tuple[str, str]] = set()

    def __call__(self, arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        returncode = 0
        stdout = ""
        if arguments[:5] == ["ip", "-4", "route", "show", "exact"]:
            subnet = arguments[5]
            stdout = f"{subnet} dev occupied\n" if subnet in self.routes else ""
        elif arguments[:4] == ["ip", "link", "show", "dev"]:
            returncode = 0 if arguments[4] in self.links else 1
        elif arguments[:4] == ["nft", "list", "table", "inet"]:
            returncode = 0 if ("inet", arguments[4]) in self.tables else 1
        elif arguments[:4] == ["ip", "tuntap", "add", "dev"]:
            self.links.add(arguments[4])
        elif arguments[:3] == ["ip", "link", "delete"]:
            self.links.discard(arguments[3])
        elif arguments[:3] == ["nft", "delete", "table"]:
            self.tables.discard((arguments[3], arguments[4]))
        elif arguments[:2] == ["nft", "-f"]:
            first_line = Path(arguments[2]).read_text(encoding="utf-8").splitlines()[0]
            _, family, table, _ = first_line.split()
            self.tables.add((family, table))
        result = subprocess.CompletedProcess(arguments, returncode, stdout, "")
        if check and returncode != 0:
            raise subprocess.CalledProcessError(returncode, arguments)
        return result


class NetworkManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.lease_path = Path(self.temporary.name) / "network-leases.json"
        self.store = FileLeaseStore(self.lease_path)
        self.runner = FakeRunner()
        self.manager = NetworkManager(self.store, runner=self.runner)

    def test_allocate_activate_and_release(self) -> None:
        lease = self.manager.allocate("agent-one")
        self.assertEqual(lease["subnet"], "172.29.0.0/30")
        self.assertEqual(lease["host_ip"], "172.29.0.1")
        self.assertEqual(lease["guest_ip"], "172.29.0.2")
        self.assertEqual(lease["status"], "reserved")

        self.manager.setup(lease, 999, (18082, 18083, 18084))
        active = self.manager.activate("agent-one")
        self.assertEqual(active["status"], "active")
        self.assertIn(lease["tap"], self.runner.links)
        self.assertIn(("inet", lease["nft_table"]), self.runner.tables)

        self.manager.release("agent-one")
        self.assertIsNone(self.store.get("agent-one"))
        self.assertNotIn(lease["tap"], self.runner.links)
        self.assertNotIn(("inet", lease["nft_table"]), self.runner.tables)

        payload = json.loads(self.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"leases": {}, "version": 1})
        self.assertEqual(stat.S_IMODE(self.lease_path.stat().st_mode), 0o600)

    def test_allocation_skips_host_route_and_existing_lease(self) -> None:
        self.runner.routes.add("172.29.0.0/30")
        first = self.manager.allocate("agent-one")
        second = self.manager.allocate("agent-two")
        self.assertEqual(first["subnet"], "172.29.0.4/30")
        self.assertEqual(second["subnet"], "172.29.0.8/30")
        self.assertNotEqual(first["tap"], second["tap"])

    def test_lab_replies_are_opt_in_and_forwarding_stays_blocked(self) -> None:
        lease = self.manager.allocate("microvm-lab")
        default_rules = self.manager.network._build_firewall_rules(lease, (18082,))
        self.assertNotIn("ct state established,related", default_rules)
        driver = LinuxNetworkDriver(self.runner, allow_host_connections=True)
        rules = driver._build_firewall_rules(lease, (18082,))
        reply = (
            f'iifname "{lease["tap"]}" ip saddr {lease["guest_ip"]} '
            f'ip daddr {lease["host_ip"]} ct state established,related counter accept'
        )
        self.assertIn(reply, rules)
        drop = f'iifname "{lease["tap"]}" ip saddr {lease["guest_ip"]} counter drop'
        self.assertLess(rules.index(reply), rules.index(drop))
        self.assertIn(f'iifname "{lease["tap"]}" counter drop', rules)
        self.assertIn(f'oifname "{lease["tap"]}" counter drop', rules)

    def test_lab_command_prepares_reads_activates_and_releases(self) -> None:
        def command(action):
            arguments = ["lab_network.py", action, "--lease-path", str(self.lease_path)]
            if action == "prepare":
                arguments.extend(["--uid", "999"])
            output = io.StringIO()
            with patch("sys.argv", arguments), patch(
                "lab_network.NetworkManager", return_value=self.manager
            ), redirect_stdout(output):
                lab_network.main()
            return output.getvalue()

        prepared = command("prepare")
        self.assertEqual(len(prepared.strip().split("\t")), 8)
        self.assertEqual(command("get"), prepared)
        command("activate")
        self.assertEqual(self.store.get("microvm-lab")["status"], "active")
        # 模拟 lab 已退出但尚未清理，重启时先清理旧租约再分配。
        self.assertEqual(command("prepare"), prepared)
        command("release")
        command("release")
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.runner.links, set())
        self.assertEqual(self.runner.tables, set())


if __name__ == "__main__":
    unittest.main()
