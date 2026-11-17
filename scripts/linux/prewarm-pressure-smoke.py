#!/usr/bin/env python3
"""阶段 5 压力验收：至少 100 轮「领取 → 停止 → 补位」真实 VM 循环。

每一轮：等待 Worker 补出一台 ready 预热机 → 通过 Control Plane HTTP API 启动
（领取并激活）→ 验证 assigned/binding/attachment → 停止（退休并释放）→ 验证
无遗留（binding/attachment/租约/占位盘/Jailer）。结束时做全量孤儿清扫。
"""

from __future__ import annotations

import http.cookiejar
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_MANAGER = REPO_ROOT / "services" / "instance-manager"
CONTROL_PLANE = REPO_ROOT / "services" / "control-plane"
sys.path[:0] = [str(INSTANCE_MANAGER), str(CONTROL_PLANE)]

from auth import AuthStore  # noqa: E402
from prewarm_config import PrewarmConfig  # noqa: E402
from prewarm_manager import PrewarmManager  # noqa: E402
from prewarm_store import PrewarmStore  # noqa: E402
from runtime import InstanceRuntime  # noqa: E402
from runtime_store import RuntimeStore, VolumeAttachmentStore  # noqa: E402


STATE_ROOT = Path("/var/lib/fc/prewarm-pressure")
DATABASE = STATE_ROOT / "control-plane.db"
VOLUME_ROOT = Path("/srv/fc/volumes/users")
PORT = 18097
BASE_URL = f"http://127.0.0.1:{PORT}"
PROFILE_KEY = "default-2vcpu-1g"
JAILER_ROOT = Path("/srv/jailer/firecracker")
PLACEHOLDER_DIR = VOLUME_ROOT.parent / "prewarm"
WORKER_LOCK = Path("/run/ai-agent-prewarm-pressure.lock")
DEFAULT_CYCLES = 100


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def request_json(opener, method: str, route: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        BASE_URL + route,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener.open(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code} {route}: {detail}") from error


def wait_for_control_plane(process: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            with urllib.request.urlopen(BASE_URL + "/healthz", timeout=0.5) as response:
                if json.load(response).get("status") == "ok":
                    return
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.1)
    tail = log_path.read_text(encoding="utf-8", errors="replace")[-8000:]
    raise RuntimeError(f"temporary Control Plane did not become ready\n{tail}")


def wait_for_ready(store: PrewarmStore, timeout: float = 240) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in store.list_ready(PROFILE_KEY):
            return item
        time.sleep(0.5)
    raise RuntimeError("no ready prewarm item within timeout")


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def cleanup_runtime(runtime: InstanceRuntime, runtime_id: str) -> None:
    try:
        status = runtime.status(runtime_id)
        if status is not None and status.get("processAlive") is True:
            runtime.stop(runtime_id, reason="prewarm_pressure_cleanup")
        runtime.destroy(runtime_id, preserve_volume=True)
    except Exception as error:
        print(f"CLEANUP_WARNING runtime={runtime_id} error={error}", file=sys.stderr)
    try:
        runtime.network_manager.release(runtime_id)
    except Exception:
        pass
    (PLACEHOLDER_DIR / f"{runtime_id}.placeholder.ext4").unlink(missing_ok=True)
    shutil.rmtree(JAILER_ROOT / runtime_id, ignore_errors=True)


def main() -> None:
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CYCLES
    if not 1 <= cycles <= 1000:
        raise RuntimeError("cycle count must be between 1 and 1000")
    if STATE_ROOT.exists():
        raise RuntimeError(f"test state already exists; inspect it first: {STATE_ROOT}")

    suffix = uuid.uuid4().hex[:10]
    logical_id = f"pressure-user-{suffix}"
    username = f"pressure-{suffix}"
    password = f"Pressure-{suffix}-{uuid.uuid4().hex[:12]}"
    credential = STATE_ROOT / "placeholder-api-key"
    config_path = STATE_ROOT / "prewarm.env"
    log_path = STATE_ROOT / "server.log"
    worker_log = STATE_ROOT / "worker.log"
    server: subprocess.Popen | None = None
    worker: subprocess.Popen | None = None
    user_id = ""
    volume_path: Path | None = None
    current_runtime_id = ""

    STATE_ROOT.mkdir(parents=True, mode=0o755)
    credential.write_text("prewarm-pressure-placeholder\n", encoding="utf-8")
    credential.chmod(0o600)
    config_path.write_text(
        "PREWARM_ENABLED=1\n"
        "PREWARM_TARGET_READY=1\n"
        "PREWARM_MAX_TOTAL=2\n"
        "PREWARM_BUILD_CONCURRENCY=1\n"
        "PREWARM_HEALTH_INTERVAL_SECONDS=1\n"
        "PREWARM_CLAIM_TIMEOUT_SECONDS=30\n"
        "PREWARM_ACTIVATE_TIMEOUT_SECONDS=15\n"
        "PREWARM_TTL_SECONDS=3600\n"
        "PREWARM_MIN_AVAILABLE_MIB=0\n"
        "PREWARM_MIN_FREE_NETWORK_SLOTS=0\n",
        encoding="utf-8",
    )

    runtime = InstanceRuntime(
        state_root=STATE_ROOT,
        volume_root=VOLUME_ROOT,
        state_database=DATABASE,
        snapshot_root=STATE_ROOT / "snapshots",
        deepseek_credential=credential,
    )
    prewarm_store = PrewarmStore(DATABASE)
    attachment_store = VolumeAttachmentStore(DATABASE)
    runtime_store = RuntimeStore(DATABASE)
    manager = PrewarmManager(
        PrewarmConfig.from_file(config_path),
        prewarm_store=prewarm_store,
        attachment_store=attachment_store,
        runtime_store=runtime_store,
        activator=runtime,
    )

    try:
        user = AuthStore(DATABASE).create_user(username, password, claim_unowned=False)
        user_id = str(user["id"])
        volume_path = runtime.user_volume_path(user_id)

        log_handle = log_path.open("ab", buffering=0)
        server = subprocess.Popen(
            [
                sys.executable,
                str(CONTROL_PLANE / "server.py"),
                "--listen-host", "127.0.0.1",
                "--listen-port", str(PORT),
                "--database", str(DATABASE),
                "--state-root", str(STATE_ROOT),
                "--deepseek-credential", str(credential),
                "--prewarm-config", str(config_path),
                "--require-auth",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_handle.close()
        wait_for_control_plane(server, log_path)

        worker_log_handle = worker_log.open("ab", buffering=0)
        worker = subprocess.Popen(
            [
                sys.executable,
                str(INSTANCE_MANAGER / "prewarm_worker_main.py"),
                "--database", str(DATABASE),
                "--state-root", str(STATE_ROOT),
                "--prewarm-config", str(config_path),
                "--profile-key", PROFILE_KEY,
                "--lock-file", str(WORKER_LOCK),
                "--max-backoff", "5",
            ],
            stdin=subprocess.DEVNULL,
            stdout=worker_log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        worker_log_handle.close()

        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        request_json(opener, "POST", "/api/auth/login", {"username": username, "password": password})
        created = request_json(opener, "POST", "/api/instances", {"id": logical_id})
        require(created.get("status") == "created", "logical instance was not created")

        first_slot = ""
        for cycle in range(1, cycles + 1):
            ready = wait_for_ready(prewarm_store)
            current_runtime_id = str(ready["runtime_id"])

            started = request_json(opener, "POST", f"/api/instances/{logical_id}/start")
            runtime_payload = started.get("runtime") or {}
            require(started.get("status") == "running", "Control Plane did not mark running")
            require(runtime_payload.get("id") == current_runtime_id, "Control Plane did not claim the ready prewarmer")

            item = prewarm_store.get(current_runtime_id)
            require(item is not None and item["status"] == "assigned", "claimed item was not assigned")
            require(runtime_store.get_binding(logical_id) is not None, "runtime binding missing")
            require(attachment_store.get(str(volume_path)) is not None, "volume attachment missing")

            stopped = request_json(opener, "POST", f"/api/instances/{logical_id}/stop")
            require(stopped.get("status") == "stopped", "Control Plane did not stop instance")
            item = prewarm_store.get(current_runtime_id)
            require(item is not None and item["status"] == "retired", "item was not retired")
            require(runtime_store.get_binding(logical_id) is None, "binding was not deleted")
            require(attachment_store.get(str(volume_path)) is None, "attachment was not released")
            require(runtime_store.get(current_runtime_id) is None, "physical runtime metadata remains")
            require(runtime.network_manager.store.get(current_runtime_id) is None, "network lease remains")
            require(not (JAILER_ROOT / current_runtime_id).exists(), "jail remains")
            require(not (PLACEHOLDER_DIR / f"{current_runtime_id}.placeholder.ext4").exists(), "placeholder remains")
            require(volume_path is not None and volume_path.is_file(), "user volume was not preserved")

            print(f"PREWARM_PRESSURE_ROUND cycle={cycle}/{cycles} status=pass runtime={current_runtime_id}")
            current_runtime_id = ""

        request_json(opener, "DELETE", f"/api/instances/{logical_id}")

        print(f"PASS: prewarm pressure test completed {cycles} claim/stop/refill cycles")
        print(
            "PREWARM_PRESSURE_READY "
            f"cycles={cycles} binding=clean attachment=clean lease=clean placeholder=clean jail=clean"
        )
    except Exception:
        if log_path.exists():
            print("--- temporary Control Plane log ---", file=sys.stderr)
            print(log_path.read_text(encoding="utf-8", errors="replace")[-12000:], file=sys.stderr)
        if worker_log.exists():
            print("--- prewarm worker log ---", file=sys.stderr)
            print(worker_log.read_text(encoding="utf-8", errors="replace")[-12000:], file=sys.stderr)
        raise
    finally:
        stop_process(worker)
        stop_process(server)
        for ready in prewarm_store.list_ready(PROFILE_KEY):
            cleanup_runtime(runtime, str(ready["runtime_id"]))
        if current_runtime_id:
            cleanup_runtime(runtime, current_runtime_id)
        if volume_path is not None:
            volume_path.unlink(missing_ok=True)
        WORKER_LOCK.unlink(missing_ok=True)
        shutil.rmtree(STATE_ROOT, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FAIL: prewarm pressure test: {error}", file=sys.stderr)
        raise SystemExit(1)
