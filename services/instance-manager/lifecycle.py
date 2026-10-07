#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


DEFAULT_STATE_ROOT = Path("/var/lib/fc")
INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
RESOURCE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def validate_instance_id(instance_id: str) -> str:
    if not INSTANCE_RE.fullmatch(instance_id):
        raise SystemExit(f"invalid instance id: {instance_id}")
    return instance_id


def paths(state_root: Path, instance_id: str) -> tuple[Path, Path, Path]:
    return (
        state_root / "instances" / f"{instance_id}.json",
        state_root / "instances" / f"{instance_id}.busy",
        state_root / "activity" / instance_id,
    )


def atomic_write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_metadata(state_root: Path, instance_id: str) -> tuple[Path, dict]:
    metadata_path, _, _ = paths(state_root, instance_id)
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SystemExit(f"instance is not registered: {instance_id}") from error
    return metadata_path, metadata


def process_start_ticks(pid: int) -> int | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return int(fields[21])
    except (FileNotFoundError, IndexError, ValueError, PermissionError):
        return None


def process_matches(metadata: dict) -> bool:
    pid = int(metadata["pid"])
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        state = fields[2]
        start_ticks = int(fields[21])
    except (FileNotFoundError, IndexError, ValueError, PermissionError):
        return False
    return state != "Z" and start_ticks == int(metadata["processStartTicks"])


def run_cleanup_command(arguments: list[str]) -> None:
    try:
        subprocess.run(
            arguments,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


def request_guest_shutdown(api_socket: Path) -> bool:
    if not api_socket.is_socket():
        return False
    payload = b'{"action_type":"SendCtrlAltDel"}'
    request = (
        b"PUT /actions HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(payload)}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
        + payload
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(api_socket))
            client.sendall(request)
            response = client.recv(128)
        return b" 204 " in response or response.startswith(b"HTTP/1.1 204")
    except OSError:
        return False


def wait_for_exit(metadata: dict, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not process_matches(metadata):
            return True
        time.sleep(0.1)
    return not process_matches(metadata)


def stop_process(metadata: dict) -> str:
    if not process_matches(metadata):
        return "already-exited"

    pid = int(metadata["pid"])
    api_socket = Path(metadata["apiSocket"])
    graceful_requested = request_guest_shutdown(api_socket)
    if graceful_requested and wait_for_exit(metadata, 3):
        return "guest-shutdown"

    if process_matches(metadata):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return "already-exited"
    if wait_for_exit(metadata, 2):
        return "sigterm"

    if process_matches(metadata):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return "already-exited"
    wait_for_exit(metadata, 1)
    return "sigkill"


def cleanup_ephemeral(metadata: dict) -> None:
    tap_name = metadata.get("tapName", "")
    if tap_name:
        if not RESOURCE_RE.fullmatch(tap_name) or len(tap_name) > 15:
            raise SystemExit("refusing to clean invalid TAP name")
        run_cleanup_command(["ip", "link", "delete", tap_name])

    nft_family = metadata.get("nftFamily", "")
    nft_table = metadata.get("nftTable", "")
    if nft_table:
        if nft_family not in {"ip", "ip6", "inet"}:
            raise SystemExit("refusing to clean invalid nftables family")
        if not RESOURCE_RE.fullmatch(nft_table):
            raise SystemExit("refusing to clean invalid nftables table")
        run_cleanup_command(["nft", "delete", "table", nft_family, nft_table])

    instance_id = metadata["id"]
    expected_jail = (Path("/srv/jailer/firecracker") / instance_id).resolve()
    jail_path = Path(metadata["jailPath"]).resolve()
    if jail_path != expected_jail:
        raise SystemExit(f"refusing to clean unexpected jail path: {jail_path}")
    shutil.rmtree(jail_path, ignore_errors=True)


def stop_instance(state_root: Path, instance_id: str, reason: str) -> dict:
    metadata_path, metadata = load_metadata(state_root, instance_id)
    stop_method = stop_process(metadata)
    cleanup_ephemeral(metadata)
    metadata.update(
        {
            "status": "stopped",
            "stopReason": reason,
            "stopMethod": stop_method,
            "stoppedAt": int(time.time()),
        }
    )
    atomic_write_json(metadata_path, metadata)
    return metadata


def command_register(args: argparse.Namespace) -> None:
    if os.geteuid() != 0:
        raise SystemExit("register must run as root")
    instance_id = validate_instance_id(args.instance_id)
    state_root = args.state_root
    metadata_path, busy_path, activity_path = paths(state_root, instance_id)
    metadata_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    activity_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)

    start_ticks = process_start_ticks(args.pid)
    if start_ticks is None:
        raise SystemExit(f"process does not exist: {args.pid}")
    if args.idle_timeout < 1:
        raise SystemExit("idle timeout must be at least one second")

    expected_jail = (Path("/srv/jailer/firecracker") / instance_id).resolve()
    if args.jail_path.resolve() != expected_jail:
        raise SystemExit(f"jail path must be {expected_jail}")
    expected_socket = (expected_jail / "root" / "firecracker.socket").resolve()
    if args.api_socket.resolve() != expected_socket:
        raise SystemExit(f"API socket must be {expected_socket}")

    volume_path = args.volume_path.resolve()
    volume_root = Path("/srv/fc/volumes").resolve()
    if volume_root not in volume_path.parents:
        raise SystemExit("volume path must be below /srv/fc/volumes")

    now = int(time.time())
    metadata = {
        "apiSocket": str(expected_socket),
        "createdAt": now,
        "id": instance_id,
        "idleTimeoutSeconds": args.idle_timeout,
        "jailPath": str(expected_jail),
        "nftFamily": args.nft_family,
        "nftTable": args.nft_table,
        "pid": args.pid,
        "processStartTicks": start_ticks,
        "status": "running",
        "tapName": args.tap_name,
        "volumePath": str(volume_path),
    }
    atomic_write_json(metadata_path, metadata)
    busy_path.unlink(missing_ok=True)
    activity_path.touch()
    os.chown(activity_path, args.activity_uid, -1)
    os.chmod(activity_path, 0o600)
    print(
        f"INSTANCE_REGISTERED id={instance_id} pid={args.pid} "
        f"idle_timeout={args.idle_timeout} volume=preserved"
    )


def command_heartbeat(args: argparse.Namespace) -> None:
    instance_id = validate_instance_id(args.instance_id)
    _, metadata = load_metadata(args.state_root, instance_id)
    _, _, activity_path = paths(args.state_root, instance_id)
    activity_path.touch()
    print(f"INSTANCE_ACTIVITY id={instance_id} status={metadata['status']}")


def command_busy(args: argparse.Namespace) -> None:
    instance_id = validate_instance_id(args.instance_id)
    load_metadata(args.state_root, instance_id)
    _, busy_path, _ = paths(args.state_root, instance_id)
    if args.busy_state == "on":
        busy_path.touch()
        os.chmod(busy_path, 0o600)
    else:
        busy_path.unlink(missing_ok=True)
    print(f"INSTANCE_BUSY id={instance_id} busy={args.busy_state}")


def command_status(args: argparse.Namespace) -> None:
    instance_id = validate_instance_id(args.instance_id)
    _, metadata = load_metadata(args.state_root, instance_id)
    _, busy_path, activity_path = paths(args.state_root, instance_id)
    last_activity = int(activity_path.stat().st_mtime) if activity_path.exists() else 0
    payload = {
        **metadata,
        "busy": busy_path.exists(),
        "idleSeconds": max(0, int(time.time()) - last_activity),
        "processAlive": process_matches(metadata),
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def command_watch(args: argparse.Namespace) -> None:
    if os.geteuid() != 0:
        raise SystemExit("watch must run as root")
    instance_id = validate_instance_id(args.instance_id)
    while True:
        _, metadata = load_metadata(args.state_root, instance_id)
        _, busy_path, activity_path = paths(args.state_root, instance_id)
        if metadata.get("status") != "running":
            return
        if not process_matches(metadata):
            cleanup_ephemeral(metadata)
            metadata_path, _ = load_metadata(args.state_root, instance_id)
            metadata.update(
                {
                    "status": "stopped",
                    "stopReason": "process_exited",
                    "stopMethod": "already-exited",
                    "stoppedAt": int(time.time()),
                }
            )
            atomic_write_json(metadata_path, metadata)
            print(f"INSTANCE_REAPED id={instance_id} reason=process_exited volume=preserved")
            return
        if busy_path.exists():
            time.sleep(args.poll_interval)
            continue
        idle_seconds = time.time() - activity_path.stat().st_mtime
        if idle_seconds >= int(metadata["idleTimeoutSeconds"]):
            stopped = stop_instance(args.state_root, instance_id, "idle_timeout")
            print(
                f"INSTANCE_REAPED id={instance_id} reason=idle_timeout "
                f"idle_seconds={int(idle_seconds)} stop={stopped['stopMethod']} volume=preserved"
            )
            return
        time.sleep(args.poll_interval)


def command_stop(args: argparse.Namespace) -> None:
    if os.geteuid() != 0:
        raise SystemExit("stop must run as root")
    instance_id = validate_instance_id(args.instance_id)
    stopped = stop_instance(args.state_root, instance_id, args.reason)
    print(
        f"INSTANCE_STOPPED id={instance_id} reason={args.reason} "
        f"stop={stopped['stopMethod']} volume=preserved"
    )


def command_destroy(args: argparse.Namespace) -> None:
    if os.geteuid() != 0:
        raise SystemExit("destroy must run as root")
    instance_id = validate_instance_id(args.instance_id)
    metadata_path, metadata = load_metadata(args.state_root, instance_id)
    if metadata.get("status") == "running":
        metadata = stop_instance(args.state_root, instance_id, "destroyed")
    volume_path = Path(metadata["volumePath"]).resolve()
    volume_root = Path("/srv/fc/volumes").resolve()
    if volume_root not in volume_path.parents:
        raise SystemExit("refusing to delete volume outside /srv/fc/volumes")
    volume_path.unlink(missing_ok=True)
    _, busy_path, activity_path = paths(args.state_root, instance_id)
    busy_path.unlink(missing_ok=True)
    activity_path.unlink(missing_ok=True)
    metadata_path.unlink(missing_ok=True)
    print(f"INSTANCE_DESTROYED id={instance_id} volume=deleted")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)

    register = commands.add_parser("register")
    register.add_argument("instance_id")
    register.add_argument("--pid", required=True, type=int)
    register.add_argument("--idle-timeout", required=True, type=int)
    register.add_argument("--jail-path", required=True, type=Path)
    register.add_argument("--api-socket", required=True, type=Path)
    register.add_argument("--volume-path", required=True, type=Path)
    register.add_argument("--tap-name", default="")
    register.add_argument("--nft-family", default="inet")
    register.add_argument("--nft-table", default="")
    register.add_argument("--activity-uid", default=0, type=int)
    register.set_defaults(handler=command_register)

    heartbeat = commands.add_parser("heartbeat")
    heartbeat.add_argument("instance_id")
    heartbeat.set_defaults(handler=command_heartbeat)

    busy = commands.add_parser("busy")
    busy.add_argument("instance_id")
    busy.add_argument("busy_state", choices=("on", "off"))
    busy.set_defaults(handler=command_busy)

    status = commands.add_parser("status")
    status.add_argument("instance_id")
    status.set_defaults(handler=command_status)

    watch = commands.add_parser("watch")
    watch.add_argument("instance_id")
    watch.add_argument("--poll-interval", default=0.25, type=float)
    watch.set_defaults(handler=command_watch)

    stop = commands.add_parser("stop")
    stop.add_argument("instance_id")
    stop.add_argument("--reason", default="manual")
    stop.set_defaults(handler=command_stop)

    destroy = commands.add_parser("destroy")
    destroy.add_argument("instance_id")
    destroy.set_defaults(handler=command_destroy)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
