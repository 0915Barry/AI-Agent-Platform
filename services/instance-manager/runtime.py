#!/usr/bin/env python3
"""把“实例启动”从回归脚本抽象为可被控制面复用的运行时。

InstanceRuntime 负责准备每个实例独立的数据盘和 jail、生成 Firecracker 配置、
启动 VMM、等待 guest 就绪，并把进程交给 lifecycle.py 管理。它刻意不处理 HTTP、
用户身份或模型消息，避免把高权限宿主操作与产品协议混在同一层。

rootfs 始终只读；用户工作区位于独立 ext4 数据盘。停止实例只清理 VMM/jail，
显式 destroy 才删除数据盘。这一语义与 M6/M8 的安全和持久化验收保持一致。
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
from network_manager import FileLeaseStore, NetworkFailure, NetworkManager


DEFAULT_STATE_ROOT = Path("/var/lib/fc")
DEFAULT_VOLUME_ROOT = Path("/srv/fc/volumes/instances")
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


class InstanceRuntime:
    """一台 Ubuntu KVM 宿主上的 Firecracker 实例运行时。"""

    def __init__(
        self,
        *,
        state_root: Path = DEFAULT_STATE_ROOT,
        volume_root: Path = DEFAULT_VOLUME_ROOT,
        idle_timeout_seconds: int = 300,
        deepseek_credential: Path | None = None,
        task_database: Path | None = None,
    ) -> None:
        self.state_root = state_root.resolve()
        self.volume_root = volume_root.resolve()
        self.idle_timeout_seconds = idle_timeout_seconds
        self.deepseek_credential = deepseek_credential.resolve() if deepseek_credential else None
        self.task_database = task_database.resolve() if task_database else None
        self.network_lease_path = self.state_root / "network-leases.json"
        # 正式受管实例需要由宿主机主动连接 Guest 的 Pi RPC 端口，因此允许
        # 已建立的 host -> Guest TCP 连接响应通过实例 nftables 规则。
        self.network_manager = NetworkManager(
            FileLeaseStore(self.network_lease_path),
            allow_host_connections=True,
        )
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
            self.state_root / "instances",
            self.state_root / "activity",
            self.state_root / "control-plane",
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o755)
        if self.deepseek_credential is not None:
            if not self.deepseek_credential.is_file():
                raise RuntimeFailure(f"DeepSeek credential is missing: {self.deepseek_credential}")
        return account.pw_uid, account.pw_gid, gateway_account.pw_uid, gateway_account.pw_gid

    def paths(self, instance_id: str) -> dict[str, Path]:
        """集中生成实例所有路径，确保它们只能落在固定根目录。"""
        instance_id = validate_instance_id(instance_id)
        jail_path = FIRECRACKER_JAIL_ROOT / instance_id
        expected_jail = (FIRECRACKER_JAIL_ROOT / instance_id).resolve()
        if jail_path.resolve() != expected_jail:
            raise RuntimeFailure("invalid jail path")
        state_dir = self.state_root / "control-plane" / instance_id
        return {
            "jail": jail_path,
            "chroot": jail_path / "root",
            "socket": jail_path / "root" / "firecracker.socket",
            "volume": self.volume_root / f"{instance_id}.ext4",
            "metadata": self.state_root / "instances" / f"{instance_id}.json",
            "activity": self.state_root / "activity" / instance_id,
            "state": state_dir,
            "console": state_dir / "console.log",
            "reaper": state_dir / "reaper.log",
            "sidecars": state_dir / "sidecars",
            "gateway_log": state_dir / "sidecars" / "gateway.log",
            "gateway_audit": state_dir / "sidecars" / "gateway.audit.jsonl",
            "credential": state_dir / "sidecars" / "deepseek-api-key",
        }

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
            *arguments,
        ]
        if capture:
            try:
                return run(command)
            except subprocess.CalledProcessError as error:
                message = (error.stderr or error.stdout or str(error)).strip()
                raise RuntimeFailure(message) from error
        return subprocess.run(command, check=False, text=True)

    def start(self, instance_id: str) -> dict:
        """启动实例并等待 AGENT_RUNTIME_READY 后再向调用者返回。

        启动成功后登记 PID 并派生独立 idle watcher。即使 HTTP 控制面重启，
        watcher 仍会在 5 分钟无活动后回收计算资源。
        """
        instance_id = validate_instance_id(instance_id)
        uid, gid, gateway_uid, gateway_gid = self.ensure_host()
        paths = self.paths(instance_id)
        existing = self.status(instance_id)
        if existing is not None and existing.get("status") == "running" and existing.get("processAlive"):
            raise RuntimeFailure(f"instance is already running: {instance_id}")

        paths["state"].mkdir(parents=True, exist_ok=True, mode=0o750)
        if self.deepseek_credential is not None:
            os.chown(paths["state"], 0, gateway_gid)
            os.chmod(paths["state"], 0o750)
        self.create_volume(paths["volume"], uid, gid)
        filesystem_check = run(["e2fsck", "-f", "-y", str(paths["volume"])], check=False)
        if filesystem_check.returncode > 1:
            raise RuntimeFailure(f"persistent volume check failed: {filesystem_check.stderr.strip()}")
        network: dict[str, str] | None = None
        sidecars: list[subprocess.Popen[bytes]] = []
        if self.deepseek_credential is not None:
            try:
                network = self.network_manager.allocate(instance_id)
                self.configure_agent_volume(paths["volume"], str(network["host_ip"]))
                paths["activity"].parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                paths["activity"].touch()
                os.chown(paths["activity"], gateway_uid, gateway_gid)
                os.chmod(paths["activity"], 0o600)
                self.network_manager.setup(
                    network,
                    uid,
                    (GATEWAY_PORT,),
                )
                sidecars = self.start_model_gateway(
                    instance_id,
                    paths,
                    network,
                    gateway_uid,
                    gateway_gid,
                )
                self.prepare_jail(instance_id, paths, network)
            except Exception as error:
                for sidecar in sidecars:
                    self.terminate_process(sidecar)
                if network is not None:
                    try:
                        self.network_manager.release(instance_id)
                    except NetworkFailure:
                        pass
                if isinstance(error, NetworkFailure):
                    raise RuntimeFailure(str(error)) from error
                raise
        else:
            self.prepare_jail(instance_id, paths)
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
                time.sleep(0.25)
            if not ready:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                tail = paths["console"].read_text(encoding="utf-8", errors="replace")[-8000:]
                raise RuntimeFailure(f"guest did not become ready\n{tail}")

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
                        "--network-lease-path",
                        str(self.network_lease_path),
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
            reaper_handle = paths["reaper"].open("ab", buffering=0)
            subprocess.Popen(
                [
                    sys.executable,
                    str(self.manager),
                    "--state-root",
                    str(self.state_root),
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
            # 回收由本进程创建的 VMM 子进程，防止停止后留下 zombie。
            threading.Thread(target=process.wait, daemon=True).start()
            for sidecar in sidecars:
                threading.Thread(target=sidecar.wait, daemon=True).start()
            status = self.status(instance_id)
            if status is None:
                raise RuntimeFailure("lifecycle registration did not create instance metadata")
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
            if isinstance(error, NetworkFailure):
                raise RuntimeFailure(str(error)) from error
            raise

    def status(self, instance_id: str) -> dict | None:
        """读取生命周期元数据，并实时计算进程存活与空闲时间。"""
        instance_id = validate_instance_id(instance_id)
        paths = self.paths(instance_id)
        if not paths["metadata"].is_file():
            return None
        try:
            metadata = json.loads(paths["metadata"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeFailure(f"invalid lifecycle metadata for {instance_id}") from error
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
        status = self.status(instance_id)
        if status is None:
            raise RuntimeFailure(f"instance is not registered: {instance_id}")
        return status

    def destroy(self, instance_id: str) -> None:
        """显式销毁实例，包括持久化数据盘和控制面运行日志目录。"""
        instance_id = validate_instance_id(instance_id)
        paths = self.paths(instance_id)
        if paths["metadata"].exists():
            self.lifecycle("destroy", instance_id)
        else:
            expected_volume_parent = self.volume_root.resolve()
            if paths["volume"].resolve().parent != expected_volume_parent:
                raise RuntimeFailure("refusing to delete a volume outside the instance volume root")
            paths["volume"].unlink(missing_ok=True)
            shutil.rmtree(paths["jail"], ignore_errors=True)
        shutil.rmtree(paths["state"], ignore_errors=True)
