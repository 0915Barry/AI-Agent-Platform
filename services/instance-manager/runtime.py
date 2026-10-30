#!/usr/bin/env python3
"""把“实例启动”从回归脚本抽象为可被控制面复用的运行时。

InstanceRuntime 负责准备每个用户唯一的数据盘和每次运行的 jail、生成 Firecracker 配置、
启动 VMM、等待 guest 就绪，并把进程交给 lifecycle.py 管理。它刻意不处理 HTTP、
用户身份或模型消息，避免把高权限宿主操作与产品协议混在同一层。

rootfs 始终只读；用户工作区位于用户专属 ext4 数据盘。停止或销毁运行实例只
清理 VMM/jail，用户盘由账户数据生命周期单独管理。
"""

import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from lifecycle import process_matches, validate_instance_id
from network_manager import NetworkFailure, NetworkManager, SQLiteLeaseStore
from runtime_store import RuntimeStore, RuntimeStoreError, UserVolumeStore


DEFAULT_STATE_ROOT = Path("/var/lib/fc")
DEFAULT_VOLUME_ROOT = Path("/srv/fc/volumes/users")
DEFAULT_SNAPSHOT_ROOT = Path("/srv/fc/snapshots/instances")
LEGACY_VOLUME_ROOT = Path("/srv/fc/volumes/instances")
ARTIFACT_ROOT = Path("/srv/fc/artifacts")
JAILER_ROOT = Path("/srv/jailer")
FIRECRACKER_JAIL_ROOT = JAILER_ROOT / "firecracker"
FIRECRACKER_USER = "firecracker"
FIRECRACKER_GROUP = "firecracker"
GATEWAY_USER = "agent-gateway"
GATEWAY_GROUP = "agent-gateway"
GATEWAY_PORT = 18082
PI_RPC_PORT = 8080


class RuntimeFailure(RuntimeError):
    """可安全转换成控制面错误响应的运行时失败。"""

    pass


def run(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """统一执行宿主命令并捕获输出，避免错误信息散落到控制台。"""
    return subprocess.run(
        arguments,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def load_version(name: str) -> str:
    """读取仓库固定版本配置；运行时不接受浮动版本或环境覆盖。"""
    versions_path = Path(__file__).resolve().parents[2] / "config" / "versions.env"
    for raw_line in versions_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == name:
            return value
    raise RuntimeFailure(f"missing {name} in {versions_path}")


# STARTUP_TIMING_DIAGNOSTIC_BEGIN
# 临时启动性能诊断。后续完成优化后，可按 STARTUP_TIMING_DIAGNOSTIC 全局搜索，
# 删除本类以及 start() 中对应的 mark()/emit() 调用，不影响实例业务逻辑。
class StartupTimingDiagnostic:
    """按启动阶段输出一行可机器检索的毫秒级耗时。"""

    STAGES = (
        "ensure_host_ms",
        "volume_create_ms",
        "e2fsck_ms",
        "network_allocate_ms",
        "volume_config_ms",
        "network_setup_ms",
        "gateway_ready_ms",
        "jail_prepare_ms",
        "guest_ready_ms",
        "lifecycle_ms",
    )

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.started_at = time.monotonic()
        self.previous_at = self.started_at
        self.values = {stage: 0 for stage in self.STAGES}
        self.emitted = False
        self.result: dict | None = None

    def mark(self, stage: str) -> None:
        now = time.monotonic()
        self.values[stage] = round((now - self.previous_at) * 1000)
        self.previous_at = now

    def emit(self, outcome: str, guest: dict[str, int] | None = None) -> dict:
        if self.emitted:
            return self.result or {}
        self.emitted = True
        total_ms = round((time.monotonic() - self.started_at) * 1000)
        self.result = {
            "outcome": outcome,
            "totalMs": total_ms,
            "host": dict(self.values),
            "guest": dict(guest or {}),
        }
        stage_values = " ".join(f"{name}={self.values[name]}" for name in self.STAGES)
        print(
            f"INSTANCE_START_TIMING instance={self.instance_id} outcome={outcome} "
            f"{stage_values} total_ms={total_ms}",
            file=sys.stderr,
            flush=True,
        )
        return self.result


def parse_guest_startup_timing(console: str) -> dict[str, int]:
    """从 Guest 串口日志提取临时启动计时，不解析其他不可信输出。"""

    result: dict[str, int] = {}
    for line in console.splitlines():
        if not line.startswith("GUEST_START_TIMING "):
            continue
        fields: dict[str, str] = {}
        for token in line.split()[1:]:
            if "=" in token:
                key, value = token.split("=", 1)
                fields[key] = value
        stage = fields.get("stage")
        value = fields.get("total_ms" if stage == "complete" else "delta_ms")
        if stage is None or value is None:
            continue
        try:
            result["total_ms" if stage == "complete" else stage] = int(value)
        except ValueError:
            continue
    return result


# STARTUP_TIMING_DIAGNOSTIC_END


class InstanceRuntime:
    """一台 Ubuntu KVM 宿主上的 Firecracker 实例运行时。"""

    def __init__(
        self,
        *,
        state_root: Path = DEFAULT_STATE_ROOT,
        volume_root: Path = DEFAULT_VOLUME_ROOT,
        idle_timeout_seconds: int = 300,
        deepseek_credential: Path | None = None,
        state_database: Path | None = None,
        snapshot_root: Path = DEFAULT_SNAPSHOT_ROOT,
    ) -> None:
        self.state_root = state_root.resolve()
        self.volume_root = volume_root.resolve()
        self.idle_timeout_seconds = idle_timeout_seconds
        self.deepseek_credential = deepseek_credential.resolve() if deepseek_credential else None
        self.state_database = (
            state_database.resolve() if state_database else self.state_root / "control-plane.db"
        )
        self.snapshot_root = snapshot_root.resolve()
        # 正式受管实例需要由宿主机主动连接 Guest 的 Pi RPC 端口，因此允许
        # 已建立的 host -> Guest TCP 连接响应通过实例 nftables 规则。
        self.network_manager = NetworkManager(
            SQLiteLeaseStore(self.state_database),
            allow_host_connections=True,
        )
        self.runtime_store = RuntimeStore(self.state_database)
        self.user_volume_store = UserVolumeStore(self.state_database)
        self.manager = Path(__file__).resolve().with_name("lifecycle.py")
        self.gateway = Path(__file__).resolve().parents[1] / "tool-gateway" / "gateway.py"
        kernel_version = load_version("SMOKE_KERNEL_VERSION")
        self.agent_uid = int(load_version("AGENT_UID"))
        self.agent_gid = int(load_version("AGENT_GID"))
        self.kernel_source = ARTIFACT_ROOT / f"vmlinux-{kernel_version}"
        self.rootfs_source = ARTIFACT_ROOT / "agent-rootfs.ext4"

    def require_root(self) -> None:
        """宿主设备、挂载、jail 与 cgroup 操作必须由 root 完成。"""
        if os.geteuid() != 0:
            raise RuntimeFailure("instance runtime must run as root")

    def ensure_host(self) -> tuple[int, int, int, int]:
        """验证构建产物和二进制，并确保专用低权限 VMM 用户存在。"""
        self.require_root()
        if not self.kernel_source.is_file():
            raise RuntimeFailure(f"guest kernel is missing: {self.kernel_source}")
        if not self.rootfs_source.is_file():
            raise RuntimeFailure(f"runtime rootfs is missing: {self.rootfs_source}")
        for executable in (Path("/usr/local/bin/firecracker"), Path("/usr/local/bin/jailer")):
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise RuntimeFailure(f"required executable is missing: {executable}")

        try:
            group = run(["getent", "group", FIRECRACKER_GROUP], check=False)
            if group.returncode != 0:
                run(["groupadd", "--system", FIRECRACKER_GROUP])
            pwd.getpwnam(FIRECRACKER_USER)
        except KeyError:
            run(
                [
                    "useradd",
                    "--system",
                    "--gid",
                    FIRECRACKER_GROUP,
                    "--no-create-home",
                    "--home-dir",
                    "/nonexistent",
                    "--shell",
                    "/usr/sbin/nologin",
                    FIRECRACKER_USER,
                ]
            )

        account = pwd.getpwnam(FIRECRACKER_USER)
        gateway_group = run(["getent", "group", GATEWAY_GROUP], check=False)
        if gateway_group.returncode != 0:
            run(["groupadd", "--system", GATEWAY_GROUP])
        try:
            pwd.getpwnam(GATEWAY_USER)
        except KeyError:
            run(
                [
                    "useradd",
                    "--system",
                    "--gid",
                    GATEWAY_GROUP,
                    "--no-create-home",
                    "--home-dir",
                    "/nonexistent",
                    "--shell",
                    "/usr/sbin/nologin",
                    GATEWAY_USER,
                ]
            )
        gateway_account = pwd.getpwnam(GATEWAY_USER)
        for directory in (
            FIRECRACKER_JAIL_ROOT,
            self.volume_root,
            LEGACY_VOLUME_ROOT,
            self.state_root / "instances",
            self.state_root / "activity",
            self.state_root / "control-plane",
            self.snapshot_root,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o755)
        if self.deepseek_credential is not None:
            if not self.deepseek_credential.is_file():
                raise RuntimeFailure(f"DeepSeek credential is missing: {self.deepseek_credential}")
        return account.pw_uid, account.pw_gid, gateway_account.pw_uid, gateway_account.pw_gid

    def paths(self, instance_id: str, volume_owner_id: str | None = None) -> dict[str, Path]:
        """集中生成实例所有路径，确保它们只能落在固定根目录。"""
        instance_id = validate_instance_id(instance_id)
        volume_owner_id = validate_instance_id(volume_owner_id or instance_id)
        jail_path = FIRECRACKER_JAIL_ROOT / instance_id
        expected_jail = (FIRECRACKER_JAIL_ROOT / instance_id).resolve()
        if jail_path.resolve() != expected_jail:
            raise RuntimeFailure("invalid jail path")
        state_dir = self.state_root / "control-plane" / instance_id
        return {
            "jail": jail_path,
            "chroot": jail_path / "root",
            "socket": jail_path / "root" / "firecracker.socket",
            "volume": self.volume_root / f"{volume_owner_id}.ext4",
            "metadata": self.state_root / "instances" / f"{instance_id}.json",
            "activity": self.state_root / "activity" / instance_id,
            "state": state_dir,
            "console": state_dir / "console.log",
            "reaper": state_dir / "reaper.log",
            "sidecars": state_dir / "sidecars",
            "gateway_log": state_dir / "sidecars" / "gateway.log",
            "gateway_audit": state_dir / "sidecars" / "gateway.audit.jsonl",
            "credential": state_dir / "sidecars" / "deepseek-api-key",
            "snapshot": self.snapshot_root / instance_id,
            "snapshot_next": self.snapshot_root / f"{instance_id}.next",
        }

    def adopt_legacy_volume(
        self, instance_id: str, owner_id: str, volume_path: Path
    ) -> None:
        """首次升级时把当前实例旧盘迁移成用户唯一数据盘。

        只迁移本次实际启动实例对应的旧盘；其他旧实例盘保持原样，避免在无法
        判断业务归属时自动合并或删除用户文件。
        """

        if volume_path.exists():
            return
        legacy_path = (LEGACY_VOLUME_ROOT / f"{validate_instance_id(instance_id)}.ext4").resolve()
        if not legacy_path.exists():
            return
        if legacy_path.parent != LEGACY_VOLUME_ROOT.resolve():
            raise RuntimeFailure("invalid legacy volume path")
        volume_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        os.replace(legacy_path, volume_path)

    def create_volume(self, volume_path: Path, uid: int, gid: int) -> None:
        """原子创建 1 GiB ext4 数据盘，并初始化 Pi 可写目录。

        先在 `.building` 文件中完成格式化和目录初始化，全部成功后再替换为正式
        数据盘，避免中断后把不完整镜像误当成可用实例数据。
        """
        if volume_path.exists():
            return
        expected_parent = self.volume_root.resolve()
        if volume_path.resolve().parent != expected_parent:
            raise RuntimeFailure("refusing to create a volume outside the instance volume root")

        building_path = volume_path.with_suffix(".ext4.building")
        mount_path = Path(tempfile.mkdtemp(prefix="agent-instance-volume.", dir="/var/tmp"))
        mounted = False
        try:
            building_path.unlink(missing_ok=True)
            run(["truncate", "-s", "1G", str(building_path)])
            run(["mkfs.ext4", "-q", "-F", "-O", "^orphan_file", "-L", "agent-data", str(building_path)])
            run(["mount", "-o", "loop", str(building_path), str(mount_path)])
            mounted = True
            for relative in ("workspace", "pi-state", "pi-state/agent"):
                directory = mount_path / relative
                directory.mkdir(parents=True, exist_ok=True, mode=0o755)
                os.chown(directory, self.agent_uid, self.agent_gid)
            run(["sync"])
            run(["umount", str(mount_path)])
            mounted = False
            os.chown(building_path, uid, gid)
            os.chmod(building_path, 0o600)
            os.replace(building_path, volume_path)
        finally:
            if mounted:
                run(["umount", str(mount_path)], check=False)
            shutil.rmtree(mount_path, ignore_errors=True)
            building_path.unlink(missing_ok=True)

    def delete_workspace_data(self, owner_id: str, directory_name: str) -> bool:
        """在 microVM 已停止时离线删除用户盘中的一个工作区目录。"""

        owner_id = validate_instance_id(owner_id)
        directory_name = validate_instance_id(directory_name)
        binding = self.user_volume_store.get(owner_id)
        if binding is None:
            return False
        volume_path = Path(binding["volume_path"]).resolve()
        if volume_path.parent != self.volume_root.resolve():
            raise RuntimeFailure("refusing to mount a user volume outside the configured root")
        if not volume_path.exists():
            return False

        filesystem_check = run(["e2fsck", "-f", "-y", str(volume_path)], check=False)
        if filesystem_check.returncode > 1:
            raise RuntimeFailure(
                f"persistent volume check failed: {filesystem_check.stderr.strip()}"
            )

        mount_path = Path(tempfile.mkdtemp(prefix="agent-workspace-delete.", dir="/var/tmp"))
        mounted = False
        deleted = False
        try:
            run(["mount", "-o", "loop", str(volume_path), str(mount_path)])
            mounted = True
            target = mount_path / "workspace" / "workspaces" / directory_name
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                target.unlink()
                deleted = True
            elif target.is_dir():
                shutil.rmtree(target)
                deleted = True
            run(["sync"])
        finally:
            if mounted:
                run(["umount", str(mount_path)], check=False)
            shutil.rmtree(mount_path, ignore_errors=True)
        return deleted

    def delete_pi_session_data(self, owner_id: str, session_id: str) -> int:
        """在 microVM 停止时删除数据盘中的指定 Pi 原生 Session 文件。"""

        owner_id = validate_instance_id(owner_id)
        session_id = validate_instance_id(session_id)
        binding = self.user_volume_store.get(owner_id)
        if binding is None:
            return 0
        volume_path = Path(binding["volume_path"]).resolve()
        if volume_path.parent != self.volume_root.resolve():
            raise RuntimeFailure("refusing to mount a user volume outside the configured root")
        if not volume_path.exists():
            return 0

        filesystem_check = run(["e2fsck", "-f", "-y", str(volume_path)], check=False)
        if filesystem_check.returncode > 1:
            raise RuntimeFailure(
                f"persistent volume check failed: {filesystem_check.stderr.strip()}"
            )

        mount_path = Path(tempfile.mkdtemp(prefix="agent-session-delete.", dir="/var/tmp"))
        mounted = False
        deleted = 0
        try:
            run(["mount", "-o", "loop", str(volume_path), str(mount_path)])
            mounted = True
            sessions_root = mount_path / "pi-state" / "agent" / "sessions"
            if sessions_root.is_dir():
                for candidate in sessions_root.rglob(f"*_{session_id}.jsonl"):
                    # rglob 结果仍做一次边界校验，避免未来目录中出现恶意符号链接。
                    resolved = candidate.resolve()
                    if not resolved.is_relative_to(sessions_root.resolve()):
                        raise RuntimeFailure("Pi session path escaped the session root")
                    if candidate.is_file() and not candidate.is_symlink():
                        candidate.unlink()
                        deleted += 1
            run(["sync"])
        finally:
            if mounted:
                run(["umount", str(mount_path)], check=False)
            shutil.rmtree(mount_path, ignore_errors=True)
        return deleted

    def configure_agent_volume(self, volume_path: Path, host_ip: str) -> None:
        """离线挂载数据盘并写入只含占位凭据的 Pi provider 配置。"""

        mount_path = Path(tempfile.mkdtemp(prefix="agent-volume-config.", dir="/var/tmp"))
        mounted = False
        try:
            run(["mount", "-o", "loop", str(volume_path), str(mount_path)])
            mounted = True
            config_dir = mount_path / "pi-state" / "agent"
            config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            models = {
                "providers": {
                    "deepseek-gateway": {
                        "baseUrl": f"http://{host_ip}:{GATEWAY_PORT}/v1",
                        "api": "openai-completions",
                        "apiKey": "gateway-placeholder",
                        "models": [
                            {
                                "id": "deepseek-flash",
                                "name": "DeepSeek Flash via Tool Gateway",
                                "reasoning": False,
                                "input": ["text"],
                                "contextWindow": 128000,
                                "maxTokens": 8192,
                            }
                        ],
                    }
                }
            }
            config_path = config_dir / "models.json"
            config_path.write_text(json.dumps(models, separators=(",", ":")) + "\n", encoding="utf-8")
            os.chown(config_dir, self.agent_uid, self.agent_gid)
            os.chown(config_path, self.agent_uid, self.agent_gid)
            os.chmod(config_path, 0o600)
            run(["sync"])
        finally:
            if mounted:
                run(["umount", str(mount_path)], check=False)
            shutil.rmtree(mount_path, ignore_errors=True)

    @staticmethod
    def terminate_process(process: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
        """尽力终止尚未交给生命周期管理器的启动阶段进程。"""

        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    def start_model_gateway(
        self,
        instance_id: str,
        paths: dict[str, Path],
        network: dict[str, str],
        gateway_uid: int,
        gateway_gid: int,
    ) -> list[subprocess.Popen[bytes]]:
        """以专用低权限用户启动当前实例的模型代理。"""

        if self.deepseek_credential is None:
            raise RuntimeFailure("managed model gateway is not configured")
        paths["sidecars"].mkdir(parents=True, exist_ok=True, mode=0o750)
        os.chown(paths["sidecars"], gateway_uid, gateway_gid)
        for log_path in (paths["gateway_log"], paths["gateway_audit"]):
            log_path.write_text("", encoding="utf-8")
            os.chown(log_path, gateway_uid, gateway_gid)
            os.chmod(log_path, 0o600)
        shutil.copyfile(self.deepseek_credential, paths["credential"])
        os.chown(paths["credential"], gateway_uid, gateway_gid)
        os.chmod(paths["credential"], 0o400)
        base = [
            "setpriv",
            "--reuid",
            str(gateway_uid),
            "--regid",
            str(gateway_gid),
            "--clear-groups",
        ]
        gateway_handle = paths["gateway_log"].open("ab", buffering=0)
        gateway_process = subprocess.Popen(
            [
                *base,
                sys.executable,
                str(self.gateway),
                "--listen-host",
                network["host_ip"],
                "--listen-port",
                str(GATEWAY_PORT),
                "--upstream",
                "https://api.deepseek.com",
                "--credential-file",
                str(paths["credential"]),
                "--audit-file",
                str(paths["gateway_audit"]),
                "--tenant",
                instance_id,
                "--activity-file",
                str(paths["activity"]),
            ],
            stdin=subprocess.DEVNULL,
            stdout=gateway_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        gateway_handle.close()
        processes = [gateway_process]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in processes):
                break
            try:
                with socket.create_connection(
                    (network["host_ip"], GATEWAY_PORT), timeout=0.2
                ):
                    pass
                return processes
            except OSError:
                pass
            time.sleep(0.2)
        for process in processes:
            self.terminate_process(process)
        gateway_tail = paths["gateway_log"].read_text(encoding="utf-8", errors="replace")[-2000:]
        raise RuntimeFailure(
            "managed model gateway did not become ready"
            f"\ngateway={gateway_tail}"
        )

    def prepare_jail(
        self,
        instance_id: str,
        paths: dict[str, Path],
        network: dict[str, str] | None = None,
    ) -> None:
        """建立 jail hard link，并生成只读 rootfs + 可写数据盘配置。"""
        shutil.rmtree(paths["jail"], ignore_errors=True)
        paths["chroot"].mkdir(parents=True, mode=0o755)
        try:
            os.link(self.kernel_source, paths["chroot"] / "vmlinux")
            os.link(self.rootfs_source, paths["chroot"] / "rootfs.ext4")
            os.link(paths["volume"], paths["chroot"] / "data.ext4")
        except OSError as error:
            shutil.rmtree(paths["jail"], ignore_errors=True)
            raise RuntimeFailure(
                "kernel, rootfs, volumes, and jailer paths must support hard links on the same Linux filesystem"
            ) from error

        boot_args = (
            "root=/dev/vda ro rootfstype=ext4 console=ttyS0 reboot=k "
            "panic=1 pci=off init=/usr/local/sbin/agent-init agent_data_disk=1"
        )
        network_interfaces: list[dict[str, str]] = []
        if network is not None:
            boot_args += (
                " agent_managed_runtime=1"
                f" agent_ip={network['guest_cidr']}"
                f" agent_gateway={network['host_ip']}"
                f" agent_dns={network['host_ip']}"
                f" agent_gateway_host={network['host_ip']}"
                f" agent_gateway_port={GATEWAY_PORT}"
                f" agent_rpc_port={PI_RPC_PORT}"
                " agent_rpc_provider=deepseek-gateway"
                " agent_rpc_model=deepseek-flash"
            )
            network_interfaces = [
                {
                    "iface_id": "eth0",
                    "guest_mac": network["guest_mac"],
                    "host_dev_name": network["tap"],
                }
            ]

        config = {
            "boot-source": {
                "kernel_image_path": "/vmlinux",
                "boot_args": boot_args,
            },
            "machine-config": {
                "vcpu_count": 2,
                "mem_size_mib": 1024,
                "smt": False,
                "track_dirty_pages": False,
                "huge_pages": "None",
            },
            "drives": [
                {
                    "drive_id": "rootfs",
                    "path_on_host": "/rootfs.ext4",
                    "is_root_device": True,
                    "is_read_only": True,
                },
                {
                    "drive_id": "data",
                    "path_on_host": "/data.ext4",
                    "is_root_device": False,
                    "is_read_only": False,
                },
            ],
            "network-interfaces": network_interfaces,
            "cpu-config": None,
            "balloon": None,
            "vsock": None,
            "logger": None,
            "metrics": None,
            "mmds-config": None,
            "entropy": None,
            "pmem": [],
            "memory-hotplug": None,
        }
        config_path = paths["chroot"] / "config.json"
        config_path.write_text(json.dumps(config, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chmod(config_path, 0o644)

    def lifecycle(self, *arguments: str, capture: bool = True) -> subprocess.CompletedProcess[str]:
        """调用统一生命周期 CLI，并把子进程错误转换为 RuntimeFailure。"""
        command = [
            sys.executable,
            str(self.manager),
            "--state-root",
            str(self.state_root),
            "--database",
            str(self.state_database),
            *arguments,
        ]
        if capture:
            try:
                return run(command)
            except subprocess.CalledProcessError as error:
                message = (error.stderr or error.stdout or str(error)).strip()
                raise RuntimeFailure(message) from error
        return subprocess.run(command, check=False, text=True)

    @staticmethod
    def firecracker_api(
        socket_path: Path, method: str, route: str, payload: dict
    ) -> None:
        """通过实例独立的 Unix API Socket 调用 Firecracker。"""

        try:
            run(
                [
                    "curl",
                    "--fail-with-body",
                    "--silent",
                    "--show-error",
                    "--unix-socket",
                    str(socket_path),
                    "--request",
                    method,
                    "--header",
                    "Content-Type: application/json",
                    "--data",
                    json.dumps(payload, separators=(",", ":")),
                    f"http://localhost{route}",
                ]
            )
        except subprocess.CalledProcessError as error:
            message = (error.stderr or error.stdout or str(error)).strip()
            raise RuntimeFailure(f"Firecracker API {method} {route} failed: {message}") from error

    @staticmethod
    def wait_for_socket(socket_path: Path, process: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if socket_path.is_socket():
                return
            if process.poll() is not None:
                break
            time.sleep(0.05)
        raise RuntimeFailure("Firecracker API socket did not become ready")

    @staticmethod
    def start_idle_watcher(manager: Path, state_root: Path, database: Path, instance_id: str, log: Path) -> None:
        """启动独立空闲回收器；冷启动和快照恢复共用相同登记规则。"""

        reaper_handle = log.open("ab", buffering=0)
        subprocess.Popen(
            [
                sys.executable,
                str(manager),
                "--state-root",
                str(state_root),
                "--database",
                str(database),
                "watch",
                instance_id,
                "--poll-interval",
                "1",
            ],
            stdin=subprocess.DEVNULL,
            stdout=reaper_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        reaper_handle.close()

    def snapshot_manifest(self, instance_id: str) -> dict:
        paths = self.paths(instance_id)
        manifest_path = paths["snapshot"] / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError) as error:
            raise RuntimeFailure(f"valid snapshot manifest is missing: {instance_id}") from error
        if manifest.get("instanceId") != instance_id or manifest.get("version") != 1:
            raise RuntimeFailure(f"invalid snapshot manifest: {instance_id}")
        return manifest

    def discard_snapshot(self, instance_id: str) -> None:
        paths = self.paths(instance_id)
        shutil.rmtree(paths["snapshot"], ignore_errors=True)
        shutil.rmtree(paths["snapshot_next"], ignore_errors=True)

    def start(self, instance_id: str, owner_id: str | None = None) -> dict:
        """启动实例并等待 AGENT_RUNTIME_READY 后再向调用者返回。

        启动成功后登记 PID 并派生独立 idle watcher。即使 HTTP 控制面重启，
        watcher 仍会在 5 分钟无活动后回收计算资源。
        """
        instance_id = validate_instance_id(instance_id)
        # STARTUP_TIMING_DIAGNOSTIC: 启动链路计时从进入 runtime.start() 开始。
        timing = StartupTimingDiagnostic(instance_id)
        uid, gid, gateway_uid, gateway_gid = self.ensure_host()
        timing.mark("ensure_host_ms")  # STARTUP_TIMING_DIAGNOSTIC
        volume_owner_id = owner_id or instance_id
        paths = self.paths(instance_id, volume_owner_id)
        existing = self.status(instance_id)
        if existing is not None and existing.get("status") == "running" and existing.get("processAlive"):
            raise RuntimeFailure(f"instance is already running: {instance_id}")

        paths["state"].mkdir(parents=True, exist_ok=True, mode=0o750)
        if self.deepseek_credential is not None:
            os.chown(paths["state"], 0, gateway_gid)
            os.chmod(paths["state"], 0o750)
        if owner_id is not None:
            self.adopt_legacy_volume(instance_id, owner_id, paths["volume"])
        self.create_volume(paths["volume"], uid, gid)
        try:
            self.user_volume_store.ensure(volume_owner_id, paths["volume"])
        except RuntimeStoreError as error:
            raise RuntimeFailure(str(error)) from error
        timing.mark("volume_create_ms")  # STARTUP_TIMING_DIAGNOSTIC
        filesystem_check = run(["e2fsck", "-f", "-y", str(paths["volume"])], check=False)
        if filesystem_check.returncode > 1:
            raise RuntimeFailure(f"persistent volume check failed: {filesystem_check.stderr.strip()}")
        timing.mark("e2fsck_ms")  # STARTUP_TIMING_DIAGNOSTIC
        network: dict[str, str] | None = None
        sidecars: list[subprocess.Popen[bytes]] = []
        if self.deepseek_credential is not None:
            try:
                network = self.network_manager.allocate(instance_id)
                timing.mark("network_allocate_ms")  # STARTUP_TIMING_DIAGNOSTIC
                self.configure_agent_volume(paths["volume"], str(network["host_ip"]))
                timing.mark("volume_config_ms")  # STARTUP_TIMING_DIAGNOSTIC
                paths["activity"].parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                paths["activity"].touch()
                os.chown(paths["activity"], gateway_uid, gateway_gid)
                os.chmod(paths["activity"], 0o600)
                self.network_manager.setup(
                    network,
                    uid,
                    (GATEWAY_PORT,),
                )
                timing.mark("network_setup_ms")  # STARTUP_TIMING_DIAGNOSTIC
                sidecars = self.start_model_gateway(
                    instance_id,
                    paths,
                    network,
                    gateway_uid,
                    gateway_gid,
                )
                timing.mark("gateway_ready_ms")  # STARTUP_TIMING_DIAGNOSTIC
                self.prepare_jail(instance_id, paths, network)
                timing.mark("jail_prepare_ms")  # STARTUP_TIMING_DIAGNOSTIC
            except Exception as error:
                for sidecar in sidecars:
                    self.terminate_process(sidecar)
                if network is not None:
                    try:
                        self.network_manager.release(instance_id)
                    except NetworkFailure:
                        pass
                timing.emit("failed")  # STARTUP_TIMING_DIAGNOSTIC
                if isinstance(error, NetworkFailure):
                    raise RuntimeFailure(str(error)) from error
                raise
        else:
            self.prepare_jail(instance_id, paths)
            timing.mark("jail_prepare_ms")  # STARTUP_TIMING_DIAGNOSTIC
        paths["console"].write_text("", encoding="utf-8")
        os.chmod(paths["console"], 0o640)

        # 让 VMM 控制台直接落盘，避免把 guest 输出混入控制面 JSON 响应。
        console_handle = paths["console"].open("ab", buffering=0)
        command = [
            "/usr/local/bin/jailer",
            "--id",
            instance_id,
            "--exec-file",
            "/usr/local/bin/firecracker",
            "--uid",
            str(uid),
            "--gid",
            str(gid),
            "--chroot-base-dir",
            str(JAILER_ROOT),
            "--cgroup-version",
            "2",
            "--resource-limit",
            "no-file=2048",
            "--",
            "--api-sock",
            "/firecracker.socket",
            "--config-file",
            "/config.json",
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=console_handle,
            stderr=subprocess.STDOUT,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
            start_new_session=True,
        )
        console_handle.close()

        ready = False
        deadline = time.monotonic() + 60
        try:
            # 以 guest 明确输出的 READY 标志作为启动成功条件，而不是只看 PID 存活。
            while time.monotonic() < deadline:
                console = paths["console"].read_text(encoding="utf-8", errors="replace")
                runtime_ready = "\nAGENT_RUNTIME_READY " in f"\n{console}"
                rpc_ready = network is None or "\nPI_RPC_READY " in f"\n{console}"
                if runtime_ready and rpc_ready:
                    ready = True
                    break
                if process.poll() is not None:
                    break
                # STARTUP_TIMING_DIAGNOSTIC: 100 ms兼顾就绪响应速度与日志读取开销。
                time.sleep(0.1)
            if not ready:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                tail = paths["console"].read_text(encoding="utf-8", errors="replace")[-8000:]
                raise RuntimeFailure(f"guest did not become ready\n{tail}")
            timing.mark("guest_ready_ms")  # STARTUP_TIMING_DIAGNOSTIC

            register_arguments = [
                "register",
                instance_id,
                "--pid",
                str(process.pid),
                "--idle-timeout",
                str(self.idle_timeout_seconds),
                "--jail-path",
                str(paths["jail"]),
                "--api-socket",
                str(paths["socket"]),
                "--volume-path",
                str(paths["volume"]),
            ]
            if network is not None:
                self.network_manager.activate(instance_id)
                register_arguments.extend(
                    [
                        "--tap-name",
                        str(network["tap"]),
                        "--nft-family",
                        "inet",
                        "--nft-table",
                        str(network["nft_table"]),
                        "--network-database",
                        str(self.state_database),
                        "--network-subnet",
                        str(network["subnet"]),
                        "--host-ip",
                        str(network["host_ip"]),
                        "--guest-ip",
                        str(network["guest_ip"]),
                        "--rpc-port",
                        str(PI_RPC_PORT),
                        "--activity-uid",
                        str(gateway_uid),
                    ]
                )
                for sidecar in sidecars:
                    register_arguments.extend(["--sidecar-pid", str(sidecar.pid)])
            self.lifecycle(*register_arguments)
            # watcher 是独立进程，因此控制面服务退出后空闲回收仍然有效。
            self.start_idle_watcher(
                self.manager,
                self.state_root,
                self.state_database,
                instance_id,
                paths["reaper"],
            )
            # 回收由本进程创建的 VMM 子进程，防止停止后留下 zombie。
            threading.Thread(target=process.wait, daemon=True).start()
            for sidecar in sidecars:
                threading.Thread(target=sidecar.wait, daemon=True).start()
            status = self.status(instance_id)
            if status is None:
                raise RuntimeFailure("lifecycle registration did not create instance metadata")
            timing.mark("lifecycle_ms")  # STARTUP_TIMING_DIAGNOSTIC
            # STARTUP_TIMING_DIAGNOSTIC: READY 之前 Guest 已把全部阶段写入串口。
            guest_timing = parse_guest_startup_timing(console)
            startup_timing = timing.emit("ready", guest_timing)
            status["startupTiming"] = startup_timing
            return status
        except Exception as error:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            for sidecar in sidecars:
                self.terminate_process(sidecar)
            if network is not None:
                try:
                    self.network_manager.release(instance_id)
                except NetworkFailure:
                    pass
            shutil.rmtree(paths["jail"], ignore_errors=True)
            timing.emit("failed")  # STARTUP_TIMING_DIAGNOSTIC
            if isinstance(error, NetworkFailure):
                raise RuntimeFailure(str(error)) from error
            raise

    def suspend(self, instance_id: str, owner_id: str) -> dict:
        """暂停运行实例、创建完整快照并释放计算与临时网络资源。"""

        instance_id = validate_instance_id(instance_id)
        owner_id = validate_instance_id(owner_id)
        uid, _, _, _ = self.ensure_host()
        paths = self.paths(instance_id, owner_id)
        metadata = self.status(instance_id)
        if metadata is None or metadata.get("status") != "running" or not metadata.get("processAlive"):
            raise RuntimeFailure(f"instance is not running: {instance_id}")
        lease = self.network_manager.store.get(instance_id)
        if lease is None:
            raise RuntimeFailure(f"network lease is missing: {instance_id}")

        source_snapshot = paths["chroot"] / "snapshot-next"
        shutil.rmtree(source_snapshot, ignore_errors=True)
        shutil.rmtree(paths["snapshot_next"], ignore_errors=True)
        source_snapshot.mkdir(mode=0o700)
        os.chown(source_snapshot, uid, pwd.getpwnam(FIRECRACKER_USER).pw_gid)

        paused = False
        started_at = time.monotonic()
        self.busy(instance_id, True)
        try:
            self.firecracker_api(paths["socket"], "PATCH", "/vm", {"state": "Paused"})
            paused = True
            snapshot_started = time.monotonic()
            self.firecracker_api(
                paths["socket"],
                "PUT",
                "/snapshot/create",
                {
                    "snapshot_type": "Full",
                    "snapshot_path": "/snapshot-next/vmstate.snap",
                    "mem_file_path": "/snapshot-next/memory.snap",
                },
            )
            snapshot_ms = round((time.monotonic() - snapshot_started) * 1000)
            if not (source_snapshot / "vmstate.snap").is_file() or not (
                source_snapshot / "memory.snap"
            ).is_file():
                raise RuntimeFailure("Firecracker did not create complete snapshot files")

            # 先把新快照移出即将清理的 jail，但旧快照仍可能是当前 VMM 的内存
            # 后端，因此必须等 VMM 退出后才能替换旧目录。
            os.replace(source_snapshot, paths["snapshot_next"])
            self.lifecycle(
                "stop",
                instance_id,
                "--reason",
                "suspended",
                "--skip-guest-shutdown",
            )
            paused = False
            shutil.rmtree(paths["snapshot"], ignore_errors=True)
            os.replace(paths["snapshot_next"], paths["snapshot"])

            build_id_path = ARTIFACT_ROOT / "agent-rootfs.build-id"
            manifest = {
                "version": 1,
                "instanceId": instance_id,
                "ownerId": owner_id,
                "createdAt": int(time.time()),
                "rootfsBuildId": build_id_path.read_text(encoding="utf-8").strip(),
                "volumePath": metadata["volumePath"],
                "network": lease,
            }
            manifest_path = paths["snapshot"] / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            os.chmod(manifest_path, 0o600)
            os.chmod(paths["snapshot"] / "vmstate.snap", 0o444)
            os.chmod(paths["snapshot"] / "memory.snap", 0o444)
            status = self.status(instance_id)
            if status is None:
                raise RuntimeFailure(f"suspended runtime metadata is missing: {instance_id}")
            status["snapshotTiming"] = {
                "totalMs": round((time.monotonic() - started_at) * 1000),
                "createMs": snapshot_ms,
            }
            return status
        except Exception:
            shutil.rmtree(paths["snapshot_next"], ignore_errors=True)
            if paused and process_matches(metadata):
                try:
                    self.firecracker_api(paths["socket"], "PATCH", "/vm", {"state": "Resumed"})
                except RuntimeFailure:
                    pass
            try:
                self.busy(instance_id, False)
            except (RuntimeFailure, subprocess.SubprocessError):
                pass
            raise

    def resume(self, instance_id: str, owner_id: str) -> dict:
        """使用完整快照恢复实例，并重新建立原网络、Gateway 和生命周期登记。"""

        instance_id = validate_instance_id(instance_id)
        owner_id = validate_instance_id(owner_id)
        started_at = time.monotonic()
        uid, gid, gateway_uid, gateway_gid = self.ensure_host()
        paths = self.paths(instance_id, owner_id)
        manifest = self.snapshot_manifest(instance_id)
        if manifest.get("ownerId") != owner_id:
            raise RuntimeFailure("snapshot owner does not match the requested user")
        build_id_path = ARTIFACT_ROOT / "agent-rootfs.build-id"
        current_build_id = build_id_path.read_text(encoding="utf-8").strip()
        if manifest.get("rootfsBuildId") != current_build_id:
            raise RuntimeFailure("snapshot rootfs version is no longer compatible")
        if str(paths["volume"].resolve()) != str(Path(manifest["volumePath"]).resolve()):
            raise RuntimeFailure("snapshot data volume does not match the user volume")
        for snapshot_file in ("vmstate.snap", "memory.snap"):
            if not (paths["snapshot"] / snapshot_file).is_file():
                raise RuntimeFailure(f"snapshot file is missing: {snapshot_file}")

        network: dict | None = None
        sidecars: list[subprocess.Popen[bytes]] = []
        process: subprocess.Popen[bytes] | None = None
        try:
            network = self.network_manager.reserve_snapshot_lease(
                instance_id, manifest["network"]
            )
            self.network_manager.setup(network, uid, (GATEWAY_PORT,))

            paths["state"].mkdir(parents=True, exist_ok=True, mode=0o750)
            os.chown(paths["state"], 0, gateway_gid)
            os.chmod(paths["state"], 0o750)
            paths["activity"].parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            paths["activity"].touch()
            os.chown(paths["activity"], gateway_uid, gateway_gid)
            os.chmod(paths["activity"], 0o600)
            gateway_started = time.monotonic()
            sidecars = self.start_model_gateway(
                instance_id, paths, network, gateway_uid, gateway_gid
            )
            gateway_ms = round((time.monotonic() - gateway_started) * 1000)

            shutil.rmtree(paths["jail"], ignore_errors=True)
            paths["chroot"].mkdir(parents=True, mode=0o755)
            os.link(self.rootfs_source, paths["chroot"] / "rootfs.ext4")
            os.link(paths["volume"], paths["chroot"] / "data.ext4")
            jail_snapshot = paths["chroot"] / "snapshot"
            jail_snapshot.mkdir(mode=0o700)
            os.chown(jail_snapshot, uid, gid)
            os.link(paths["snapshot"] / "vmstate.snap", jail_snapshot / "vmstate.snap")
            os.link(paths["snapshot"] / "memory.snap", jail_snapshot / "memory.snap")

            paths["console"].write_text("", encoding="utf-8")
            os.chmod(paths["console"], 0o640)
            console_handle = paths["console"].open("ab", buffering=0)
            process_started = time.monotonic()
            process = subprocess.Popen(
                [
                    "/usr/local/bin/jailer",
                    "--id",
                    instance_id,
                    "--exec-file",
                    "/usr/local/bin/firecracker",
                    "--uid",
                    str(uid),
                    "--gid",
                    str(gid),
                    "--chroot-base-dir",
                    str(JAILER_ROOT),
                    "--cgroup-version",
                    "2",
                    "--resource-limit",
                    "no-file=2048",
                    "--",
                    "--api-sock",
                    "/firecracker.socket",
                ],
                stdin=subprocess.DEVNULL,
                stdout=console_handle,
                stderr=subprocess.STDOUT,
                env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
                start_new_session=True,
            )
            console_handle.close()
            self.wait_for_socket(paths["socket"], process)
            process_ms = round((time.monotonic() - process_started) * 1000)

            load_started = time.monotonic()
            self.firecracker_api(
                paths["socket"],
                "PUT",
                "/snapshot/load",
                {
                    "snapshot_path": "/snapshot/vmstate.snap",
                    "mem_backend": {
                        "backend_path": "/snapshot/memory.snap",
                        "backend_type": "File",
                    },
                    "track_dirty_pages": False,
                    "resume_vm": False,
                },
            )
            load_ms = round((time.monotonic() - load_started) * 1000)
            resumed_at = time.monotonic()
            self.firecracker_api(paths["socket"], "PATCH", "/vm", {"state": "Resumed"})
            resume_api_ms = round((time.monotonic() - resumed_at) * 1000)

            self.network_manager.activate(instance_id)
            register_arguments = [
                "register",
                instance_id,
                "--pid",
                str(process.pid),
                "--idle-timeout",
                str(self.idle_timeout_seconds),
                "--jail-path",
                str(paths["jail"]),
                "--api-socket",
                str(paths["socket"]),
                "--volume-path",
                str(paths["volume"]),
                "--tap-name",
                str(network["tap"]),
                "--nft-family",
                "inet",
                "--nft-table",
                str(network["nft_table"]),
                "--network-database",
                str(self.state_database),
                "--network-subnet",
                str(network["subnet"]),
                "--host-ip",
                str(network["host_ip"]),
                "--guest-ip",
                str(network["guest_ip"]),
                "--rpc-port",
                str(PI_RPC_PORT),
                "--activity-uid",
                str(gateway_uid),
            ]
            for sidecar in sidecars:
                register_arguments.extend(["--sidecar-pid", str(sidecar.pid)])
            self.lifecycle(*register_arguments)
            self.start_idle_watcher(
                self.manager,
                self.state_root,
                self.state_database,
                instance_id,
                paths["reaper"],
            )
            threading.Thread(target=process.wait, daemon=True).start()
            for sidecar in sidecars:
                threading.Thread(target=sidecar.wait, daemon=True).start()
            status = self.status(instance_id)
            if status is None:
                raise RuntimeFailure("snapshot restore did not register runtime metadata")
            status["resumeTiming"] = {
                "totalMs": round((time.monotonic() - started_at) * 1000),
                "gatewayMs": gateway_ms,
                "processMs": process_ms,
                "loadMs": load_ms,
                "resumeApiMs": resume_api_ms,
            }
            return status
        except Exception as error:
            if process is not None and process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            for sidecar in sidecars:
                self.terminate_process(sidecar)
            if network is not None:
                try:
                    self.network_manager.release(instance_id)
                except NetworkFailure:
                    pass
            shutil.rmtree(paths["jail"], ignore_errors=True)
            if isinstance(error, NetworkFailure):
                raise RuntimeFailure(str(error)) from error
            raise

    def status(self, instance_id: str) -> dict | None:
        """读取生命周期元数据，并实时计算进程存活与空闲时间。"""
        instance_id = validate_instance_id(instance_id)
        paths = self.paths(instance_id)
        try:
            metadata = self.runtime_store.load_legacy(paths["metadata"], instance_id)
        except RuntimeStoreError as error:
            raise RuntimeFailure(f"invalid lifecycle metadata for {instance_id}") from error
        if metadata is None:
            return None
        activity = int(paths["activity"].stat().st_mtime) if paths["activity"].exists() else 0
        metadata["processAlive"] = process_matches(metadata)
        metadata["idleSeconds"] = max(0, int(time.time()) - activity)
        return metadata

    def heartbeat(self, instance_id: str) -> dict:
        """刷新实例活动时间，并返回刷新后的运行状态。"""
        self.lifecycle("heartbeat", validate_instance_id(instance_id))
        status = self.status(instance_id)
        if status is None:
            raise RuntimeFailure(f"instance is not registered: {instance_id}")
        return status

    def busy(self, instance_id: str, enabled: bool) -> None:
        """执行任务期间设置 busy 标记，防止空闲回收器中途关闭实例。"""

        self.lifecycle("busy", validate_instance_id(instance_id), "on" if enabled else "off")

    def stop(self, instance_id: str, reason: str = "api") -> dict:
        """停止计算资源但保留实例记录和持久化数据盘。"""
        instance_id = validate_instance_id(instance_id)
        self.lifecycle("stop", instance_id, "--reason", reason)
        self.discard_snapshot(instance_id)
        status = self.status(instance_id)
        if status is None:
            raise RuntimeFailure(f"instance is not registered: {instance_id}")
        return status

    def destroy(self, instance_id: str) -> None:
        """销毁一次运行实例，但保留归属于用户的持久化数据盘。"""
        instance_id = validate_instance_id(instance_id)
        paths = self.paths(instance_id)
        if self.status(instance_id) is not None:
            self.lifecycle("destroy", instance_id, "--preserve-volume")
        else:
            shutil.rmtree(paths["jail"], ignore_errors=True)
        shutil.rmtree(paths["state"], ignore_errors=True)
        self.discard_snapshot(instance_id)
