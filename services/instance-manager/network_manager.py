#!/usr/bin/env python3
"""为单台 Linux 宿主机分配 Firecracker 私网并管理网络租约。

这个文件故意按职责分成五个部分：

1. LeaseStore：定义租约存储需要提供哪些操作；
2. FileLeaseStore：只负责把租约保存到 JSON 文件；
3. NetworkAddressPool：只负责计算 /30 地址槽位；
4. LinuxNetworkDriver：只负责执行 ip 和 nft 命令；
5. NetworkManager：按正确顺序组织上面几个组件。

当前使用 JSON 文件保存租约。将来换成数据库时，只需要增加新的 LeaseStore
实现，不需要改地址计算和 Linux 网络配置代码。
"""

from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


DEFAULT_NETWORK_POOL = "172.29.0.0/16"
DEFAULT_PREFIX_LENGTH = 30
VALID_RESOURCE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class NetworkFailure(RuntimeError):
    """网络槽位、Linux 网络资源或租约状态不一致。"""


class LeaseConflict(NetworkFailure):
    """另一个启动流程已经占用了实例或网络槽位。"""


class NetworkRollbackFailure(NetworkFailure):
    """网络创建失败后，仍有 Linux 网络资源没有清理干净。"""


# ---------------------------------------------------------------------------
# 1. 租约存储接口
# ---------------------------------------------------------------------------


class LeaseStore:
    """租约存储的最小接口。

    这里使用普通 Python 基类，不依赖抽象类或 Protocol。数据库版本只要实现
    下面五个方法，就可以替换 FileLeaseStore。
    """

    def list(self) -> list[dict]:
        raise NotImplementedError

    def get(self, instance_id: str) -> dict | None:
        raise NotImplementedError

    def reserve(self, lease: dict) -> dict:
        raise NotImplementedError

    def set_status(self, instance_id: str, status: str) -> dict:
        raise NotImplementedError

    def release(self, instance_id: str) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 2. JSON 文件租约存储
# ---------------------------------------------------------------------------


class FileLeaseStore(LeaseStore):
    """使用 JSON 文件保存租约，并用 flock 防止并发写坏文件。"""

    def __init__(self, path: Path) -> None:
        # 目前网络租约没有存进数据库，是用 JSON 文件保存
        self.path = path.resolve()
        # 用 suffix 加个 .lock 的后缀
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def _file_lock(self):
        """在一次完整的读取或修改过程中持有独占文件锁。"""
        # 目录/var/lib/fc/不存在就创建
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            # 设置一下权限
            os.chmod(self.lock_path, 0o600)
            # 向操作系统申请文件锁，取得打开文件的编号lock_file.fileno()，操作系统用这个编号识别文件
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                # LOCK_UN：释放锁，让等待的其他进程有机会取得锁
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_file(self) -> dict:
        """读取租约 JSON 文件，把内容转换成 Python 字典"""
        try:
            # 把 JSON 字符串转换成 Python 字典
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # 文件不存在
            return {"version": 1, "leases": {}}
        except (OSError, json.JSONDecodeError) as error:
            # 不是json格式
            raise NetworkFailure(f"invalid network lease file: {self.path}") from error

        # 判断租约的键是否有以及字典格式正确与否
        if payload.get("version") != 1 or not isinstance(payload.get("leases"), dict):
            raise NetworkFailure(f"unsupported network lease file: {self.path}")
        return payload

    def _write_file(self, payload: dict) -> None:
        """
        先写临时文件再替换正式文件，避免中途退出留下半个JSON。
        实际上就是保存租约文件到硬盘中
        """
        # tempfile.mkstemp 创建并打开一个临时文件
        # handle: 打开文件的编号
        # temporary_name: 临时文件的完整路径字符串
        handle, temporary_name = tempfile.mkstemp(
            prefix=self.path.name + ".",  # 文件名前缀：network-leases.json.
            suffix=".tmp",               # 文件名后缀：.tmp
            dir=self.path.parent,        # 所在目录：与正式租约文件相同
        )
        # 转化成地址对象
        temporary_path = Path(temporary_name)
        try:
            # 根据整数编号打开
            with os.fdopen(handle, "w", encoding="utf-8") as output:
                # 字典转化成json
                json.dump(payload, output, sort_keys=True, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def list(self) -> list[dict]:
        """读取全部租约记录"""
        with self._file_lock():
            payload = self._read_file()
            return [dict(lease) for lease in payload["leases"].values()]

    def get(self, instance_id: str) -> dict | None:
        """查询某个实例的租约"""
        with self._file_lock():
            payload = self._read_file()
            lease = payload["leases"].get(instance_id)
            return dict(lease) if lease is not None else None

    def reserve(self, lease: dict) -> dict:
        """
        把传进来的配置登记成租约
        如果实例、子网或 TAP 名称已被登记，就拒绝
        """
        instance_id = str(lease["instance_id"])
        with self._file_lock():
            payload = self._read_file()
            leases = payload["leases"]

            if instance_id in leases:
                raise LeaseConflict(f"instance already has a network lease: {instance_id}")

            for existing in leases.values():
                if existing.get("subnet") == lease.get("subnet"):
                    raise LeaseConflict(f"network slot is already leased: {lease['subnet']}")
                if existing.get("tap") == lease.get("tap"):
                    raise LeaseConflict(f"TAP name is already leased: {lease['tap']}")

            now = int(time.time())
            stored = dict(lease)
            stored["status"] = "reserved"
            stored["created_at"] = now
            stored["updated_at"] = now
            leases[instance_id] = stored
            self._write_file(payload)
            return dict(stored)

    def set_status(self, instance_id: str, status: str) -> dict:
        """更新租约状态"""
        allowed_statuses = {"reserved", "active", "releasing", "error"}
        if status not in allowed_statuses:
            raise NetworkFailure(f"invalid network lease status: {status}")

        with self._file_lock():
            payload = self._read_file()
            lease = payload["leases"].get(instance_id)
            if lease is None:
                raise NetworkFailure(f"network lease does not exist: {instance_id}")

            lease["status"] = status
            lease["updated_at"] = int(time.time())
            self._write_file(payload)
            return dict(lease)

    def release(self, instance_id: str) -> None:
        """从文件中删除这个实例的租约记录"""
        with self._file_lock():
            payload = self._read_file()
            if instance_id in payload["leases"]:
                del payload["leases"][instance_id]
                self._write_file(payload)


# ---------------------------------------------------------------------------
# 3. 地址池计算
# ---------------------------------------------------------------------------


class NetworkAddressPool:
    """
    把一个 IPv4 地址池切分为固定大小的实例网络槽位。
    不统计已经用了多少槽位 / 只是是初始化时算好怎么划分地址池
    通过 build_lease() 生成某个槽位的配置
    """

    def __init__(self, pool: str, prefix_length: int) -> None:
        # 调用 Python 自带的 ipaddress 模块中的 ip_network() 函数，
        # 把地址池字符串（如 "172.29.0.0/16"）转换成网络对象，
        # 方便后续读取 IP 版本、前缀长度和网络起始地址；不会配置真实网络。
        # network.version          # 4，表示 IPv4
        # network.prefixlen        # 16，即斜杠后面的数字
        # network.network_address  # 172.29.0.0，网络起始地址
        self.network = ipaddress.ip_network(pool)
        self.prefix_length = prefix_length

        # 检查 ip 和子网掩码合规
        if self.network.version != 4:
            raise NetworkFailure("network pool must be IPv4")
        if prefix_length <= self.network.prefixlen or prefix_length > 30:
            raise NetworkFailure("network pool must contain /30-or-larger slots")


        # 计算槽位数量
        # 2 的 14 次方 = 16384 个槽位
        self.slot_count = 1 << (prefix_length - self.network.prefixlen)
        # 计算每个槽位的地址数量
        self.block_size = 1 << (32 - prefix_length)

    # 根据实例名字和槽位编号，生成网络配置
    def build_lease(self, instance_id: str, slot: int) -> dict:
        """根据槽位编号生成子网、Host IP、Guest IP、TAP 和 nft 表名。"""

        # self.network.network_address：整个地址池的起始地址，默认是 172.29.0.0。
        # int(...)：把 IP 地址转成整数，方便加减。
        # slot * self.block_size：计算需要往后移动多少个地址。
        address = int(self.network.network_address) + slot * self.block_size
        # 把算出的起始地址与前缀长度 30 组合成网络对象
        subnet = ipaddress.ip_network((address, self.prefix_length))

        # 起始地址：172.29.0.4
        # 加 1： 172.29.0.5 → Host，即 Ubuntu 宿主机
        # 加 2： 172.29.0.6 → Guest，即 microVM
        # subnet.network_address  得到 172.29.0.0
        host_ip = str(subnet.network_address + 1)
        guest_ip = str(subnet.network_address + 2)

        return {
            "instance_id": instance_id,
            "slot": slot,
            "subnet": str(subnet),
            "host_ip": host_ip,
            "host_cidr": f"{host_ip}/{self.prefix_length}",
            "guest_ip": guest_ip,
            "guest_cidr": f"{guest_ip}/{self.prefix_length}",
            "guest_mac": f"06:00:ac:1d:{(slot >> 8) & 0xff:02x}:{slot & 0xff:02x}",
            "tap": f"fc{slot:08x}",
            "nft_table": f"aap_{slot:08x}",
            "nft_family": "inet",
        }


# ---------------------------------------------------------------------------
# 4. Linux 网络操作
# ---------------------------------------------------------------------------


def run_command(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """直接执行命令，不经过 shell；测试时可以换成内存版 runner。"""
    return subprocess.run(
        arguments,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class LinuxNetworkDriver:
    """只负责创建和删除 Linux TAP、地址、路由及 nftables 规则。"""
    # 记住用什么工具执行 Linux 命令，以及是否允许宿主机连接的响应
    def __init__(self, runner=None, *, allow_host_connections: bool = False) -> None:
        self.runner = runner or run_command

        # 记住是否允许 Guest 回复宿主机主动建立的连接
        self.allow_host_connections = allow_host_connections

    def _resource_exists(self, arguments: list[str]) -> bool:
        """执行查询命令，判断资源是否存在"""

        # arguments 是命令及参数组成的列表 ["ip", "link", "show", "dev", "fc00000000"]
        result = self.runner(arguments, check=False)
        return result.returncode == 0

    def _validate_lease(self, lease: dict) -> None:
        """检查准备使用的资源名称是否合法"""
        tap = str(lease["tap"])
        table = str(lease["nft_table"])
        family = str(lease.get("nft_family", "inet"))

        if len(tap) > 15 or not VALID_RESOURCE_NAME.fullmatch(tap):
            raise NetworkFailure(f"invalid TAP name: {tap}")
        if not VALID_RESOURCE_NAME.fullmatch(table):
            raise NetworkFailure(f"invalid nftables table name: {table}")
        if family != "inet":
            raise NetworkFailure(f"unsupported nftables family: {family}")

    def resources_are_free(self, lease: dict) -> bool:
        """检查租约对应的路由、TAP 和 nftables 表是否都未被占用。"""
        self._validate_lease(lease)

        route = self.runner(
            ["ip", "-4", "route", "show", "exact", str(lease["subnet"])],
            check=False,
        )
        if route.stdout.strip():
            return False

        tap_exists = self._resource_exists(
            ["ip", "link", "show", "dev", str(lease["tap"])]
        )
        if tap_exists:
            return False

        table_exists = self._resource_exists(
            [
                "nft",
                "list",
                "table",
                str(lease["nft_family"]),
                str(lease["nft_table"]),
            ]
        )
        return not table_exists

    def _build_firewall_rules(self, lease: dict, allowed_host_ports: tuple[int, ...]) -> str:
        """生成防火墙规则文本，规定哪些流量可以通过，此时还没有安装规则。"""
        ports = tuple(sorted({int(port) for port in allowed_host_ports}))
        if not ports or any(port < 1 or port > 65535 for port in ports):
            raise NetworkFailure("allowed host ports must contain valid TCP ports")

        port_set = ", ".join(str(port) for port in ports)
        # 教学 lab 允许宿主机直接连接 Guest RPC，需要接收该连接的响应。
        host_reply_rule = ""
        if self.allow_host_connections:
            host_reply_rule = (
                f'    iifname "{lease["tap"]}" ip saddr {lease["guest_ip"]} '
                f'ip daddr {lease["host_ip"]} ct state established,related counter accept\n'
            )
        return f"""table inet {lease['nft_table']} {{
  chain input {{
    type filter hook input priority -10; policy accept;
    iifname \"{lease['tap']}\" ip saddr {lease['guest_ip']} ip daddr {lease['host_ip']} tcp dport {{ {port_set} }} counter accept
{host_reply_rule}    iifname \"{lease['tap']}\" ip saddr {lease['guest_ip']} counter drop
  }}
  chain forward {{
    type filter hook forward priority -10; policy accept;
    iifname \"{lease['tap']}\" counter drop
    oifname \"{lease['tap']}\" counter drop
  }}
}}
"""

    def _install_firewall(self, rules: str) -> None:
        """将规则写入临时文件，执行 nft 命令，让规则生效。"""
        with tempfile.NamedTemporaryFile(
            "w",
            prefix="agent-network.",
            suffix=".nft",
            delete=False,
        ) as rules_file:
            rules_file.write(rules)
            rules_path = Path(rules_file.name)

        try:
            self.runner(["nft", "-f", str(rules_path)])
        finally:
            rules_path.unlink(missing_ok=True)

    def _delete_firewall(self, lease: dict) -> None:
        """删除该实例的防火墙表。"""
        family = str(lease.get("nft_family", "inet"))
        table = str(lease["nft_table"])
        if self._resource_exists(["nft", "list", "table", family, table]):
            self.runner(["nft", "delete", "table", family, table], check=False)

    def _delete_tap(self, lease: dict) -> None:
        """删除该实例的 TAP 设备，它上面的 IP 和关联的直连路由也会随之移除。"""
        tap = str(lease["tap"])
        if self._resource_exists(["ip", "link", "show", "dev", tap]):
            self.runner(["ip", "link", "delete", tap], check=False)

    def _find_leftovers(self, lease: dict) -> list[str]:
        """检查 TAP、防火墙表是否还有残留，返回残留名称列表。"""
        leftovers = []
        family = str(lease.get("nft_family", "inet"))
        table = str(lease["nft_table"])
        tap = str(lease["tap"])

        if self._resource_exists(["nft", "list", "table", family, table]):
            leftovers.append(f"nft:{family}/{table}")
        if self._resource_exists(["ip", "link", "show", "dev", tap]):
            leftovers.append(f"tap:{tap}")
        return leftovers

    def create(self, lease: dict, firecracker_uid: int, allowed_host_ports: tuple[int, ...]) -> None:
        """
        组织创建流程：检查配置和占用情况 → 创建 TAP → 配置宿主 IP → 启用 TAP → 安装防火墙；中途失败则尝试撤销。
        """
        self._validate_lease(lease)
        rules = self._build_firewall_rules(lease, allowed_host_ports)

        if not self.resources_are_free(lease):
            raise NetworkFailure(f"network resources are no longer free: {lease['subnet']}")

        tap_created = False
        firewall_attempted = False
        try:
            self.runner(
                [
                    "ip",
                    "tuntap",
                    "add",
                    "dev",
                    str(lease["tap"]),
                    "mode",
                    "tap",
                    "user",
                    str(firecracker_uid),
                ]
            )
            tap_created = True
            self.runner(
                ["ip", "address", "add", str(lease["host_cidr"]), "dev", str(lease["tap"])]
            )
            self.runner(["ip", "link", "set", "dev", str(lease["tap"]), "up"])

            firewall_attempted = True
            self._install_firewall(rules)
        except Exception as error:
            if firewall_attempted:
                self._delete_firewall(lease)
            if tap_created:
                self._delete_tap(lease)

            leftovers = self._find_leftovers(lease)
            if leftovers:
                message = "network setup rollback failed: " + ", ".join(leftovers)
                raise NetworkRollbackFailure(message) from error
            raise

    def remove(self, lease: dict) -> list[str]:
        """组织删除流程：删除防火墙表 → 删除 TAP → 检查并返回残留。"""
        self._validate_lease(lease)
        self._delete_firewall(lease)
        self._delete_tap(lease)
        return self._find_leftovers(lease)


# ---------------------------------------------------------------------------
# 5. 网络租约编排
# ---------------------------------------------------------------------------


class NetworkManager:
    """只负责编排：分配租约、创建网络、激活租约和释放网络。"""

    def __init__(
        self,
        store: LeaseStore,
        *,
        pool: str = DEFAULT_NETWORK_POOL,
        # 每个槽位的前缀长度
        prefix_length: int = DEFAULT_PREFIX_LENGTH,
        runner=None,
        allow_host_connections: bool = False,
    ) -> None:
        # 记住传进来的存储工具，以后用它读取和保存租约
        self.store = store
        # 创建地址计算器
        self.address_pool = NetworkAddressPool(pool, prefix_length)
        # 创建 Linux 网络操作工具
        self.network = LinuxNetworkDriver(runner, allow_host_connections=allow_host_connections)

    def allocate(self, instance_id: str) -> dict:
        """按槽位顺序查找并预留第一个可用的 /30 网络。"""

        # 先检查是不是这个实例已经有租约
        existing = self.store.get(instance_id)
        if existing is not None:
            raise NetworkFailure(
                f"instance has an unreleased network lease: "
                f"{instance_id} status={existing['status']}"
            )
        # 收集 slot 成一个列表
        used_slots = {int(lease["slot"]) for lease in self.store.list()}
        for slot in range(self.address_pool.slot_count):
            if slot in used_slots:
                continue

            # 生成这个槽位的候选配置
            candidate = self.address_pool.build_lease(instance_id, slot)
            if not self.network.resources_are_free(candidate):
                continue

            try:
                return self.store.reserve(candidate)
            except LeaseConflict:
                # 并发启动时，另一个流程可能刚好抢先占用了这个槽位。
                continue

        raise NetworkFailure(f"no free network slot remains in {self.address_pool.network}")

    def setup(self, lease: dict, firecracker_uid: int, allowed_host_ports: tuple[int, ...]) -> None:
        """根据已经分配的租约创建真实网络，并在创建失败时处理租约记录。失败时释放租约或保留 error 记录。"""
        instance_id = str(lease["instance_id"])
        try:
            # 真正创建网络
            self.network.create(lease, firecracker_uid, allowed_host_ports)
        except NetworkRollbackFailure:
            self.store.set_status(instance_id, "error")
            raise
        except Exception:
            self.store.release(instance_id)
            raise

    def activate(self, instance_id: str) -> dict:
        """Guest 检查通过后，把 reserved 租约标记为 active。"""
        return self.store.set_status(instance_id, "active")

    def release(self, instance_id: str, *, lease: dict | None = None) -> None:
        """删除 Linux 网络资源；完全清理后再删除租约记录。"""
        # 从 JSON 账本里查到 microvm-lab 的网络配置
        stored_lease = self.store.get(instance_id)
        # 这里要确认一下两个记录是不是都没有
        effective_lease = stored_lease or lease
        # 都没有就直接返回结束
        if effective_lease is None:
            return
        # 只要账本里有记录，就把状态从 active 改成 releasing
        if stored_lease is not None:
            self.store.set_status(instance_id, "releasing")

        # 调用 Linux 网络工具删除 TAP 和防火墙表，并返回“没有删除干净的资源”。
        leftovers = self.network.remove(effective_lease)

        # 看看 leftovers 里面有没有残留记录
        if leftovers:
            if stored_lease is not None:
                self.store.set_status(instance_id, "error")
            message = "network cleanup failed: " + ", ".join(leftovers)
            raise NetworkFailure(message)
        # 执行到这里，说明没有残留，就从 JSON 中删掉这个实例的租约。
        self.store.release(instance_id)
