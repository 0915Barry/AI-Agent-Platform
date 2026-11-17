#!/usr/bin/env python3
"""验证常驻 Worker 会发现 ready Firecracker 死亡并自动补位。"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_MANAGER = REPO_ROOT / "services" / "instance-manager"
WORKER_MAIN = INSTANCE_MANAGER / "prewarm_worker_main.py"
sys.path.insert(0, str(INSTANCE_MANAGER))

from prewarm_store import PrewarmStore  # noqa: E402
from runtime import InstanceRuntime  # noqa: E402


STATE_ROOT = Path("/var/lib/fc/prewarm-worker-health-smoke")
DATABASE = STATE_ROOT / "control-plane.db"
CONFIG = STATE_ROOT / "prewarm.env"
LOG = STATE_ROOT / "worker.log"
LOCK = Path("/run/ai-agent-prewarm-worker-health-smoke.lock")
PROFILE_KEY = "default-2vcpu-1g"
VOLUME_ROOT = Path("/srv/fc/volumes/users")
JAILER_ROOT = Path("/srv/jailer/firecracker")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def placeholder(runtime_id: str) -> Path:
    return VOLUME_ROOT.parent / "prewarm" / f"{runtime_id}.placeholder.ext4"


def all_runtime_ids(store: PrewarmStore) -> list[str]:
    with store.connect() as connection:
        rows = connection.execute("SELECT runtime_id FROM prewarm_items").fetchall()
    return [str(row["runtime_id"]) for row in rows]


def cleanup_runtime(runtime: InstanceRuntime, store: PrewarmStore, runtime_id: str) -> None:
    try:
        item = store.get(runtime_id)
        if item is not None and item.get("status") == "preparing":
            store.mark_prepare_error(runtime_id, "health smoke cleanup")
            item = store.get(runtime_id)
        if item is not None and item.get("status") in {"ready", "error"}:
            store.mark_draining(runtime_id)
        status = runtime.status(runtime_id)
        if status is not None and status.get("processAlive") is True:
            runtime.stop(runtime_id, reason="prewarm_health_smoke_cleanup")
        runtime.destroy(runtime_id, preserve_volume=True)
        item = store.get(runtime_id)
        if item is not None and item.get("status") in {"draining", "error"}:
            store.retire(runtime_id)
    except Exception as error:
        print(f"CLEANUP_WARNING runtime={runtime_id} error={error}", file=sys.stderr)
    try:
        runtime.network_manager.release(runtime_id)
    except Exception:
        pass
    placeholder(runtime_id).unlink(missing_ok=True)
    shutil.rmtree(JAILER_ROOT / runtime_id, ignore_errors=True)


def wait_first_ready(store: PrewarmStore, worker: subprocess.Popen) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if worker.poll() is not None:
            break
        ready = store.list_ready(PROFILE_KEY)
        if len(ready) == 1:
            return ready[0]
        time.sleep(0.1)
    tail = LOG.read_text(encoding="utf-8", errors="replace")[-8000:] if LOG.exists() else ""
    raise RuntimeError(f"Worker did not create the first ready item\n{tail}")


def wait_replacement(
    store: PrewarmStore,
    worker: subprocess.Popen,
    failed_runtime_id: str,
) -> dict:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if worker.poll() is not None:
            break
        failed = store.get(failed_runtime_id)
        ready = [
            item
            for item in store.list_ready(PROFILE_KEY)
            if item["runtime_id"] != failed_runtime_id
        ]
        if failed is not None and failed.get("status") == "retired" and len(ready) == 1:
            return ready[0]
        time.sleep(0.1)
    tail = LOG.read_text(encoding="utf-8", errors="replace")[-12000:] if LOG.exists() else ""
    raise RuntimeError(f"Worker did not retire and replace the failed ready item\n{tail}")


def main() -> None:
    if STATE_ROOT.exists():
        raise RuntimeError(f"test state already exists; inspect it first: {STATE_ROOT}")
    STATE_ROOT.mkdir(parents=True, mode=0o755)
    LOCK.unlink(missing_ok=True)
    CONFIG.write_text(
        "PREWARM_ENABLED=1\n"
        "PREWARM_TARGET_READY=1\n"
        "PREWARM_MAX_TOTAL=1\n"
        "PREWARM_BUILD_CONCURRENCY=1\n"
        "PREWARM_TTL_SECONDS=300\n"
        "PREWARM_HEALTH_INTERVAL_SECONDS=1\n"
        "PREWARM_MIN_AVAILABLE_MIB=0\n"
        "PREWARM_MIN_FREE_NETWORK_SLOTS=0\n",
        encoding="utf-8",
    )
    runtime = InstanceRuntime(
        state_root=STATE_ROOT,
        volume_root=VOLUME_ROOT,
        state_database=DATABASE,
        snapshot_root=STATE_ROOT / "snapshots",
    )
    store = PrewarmStore(DATABASE)
    worker: subprocess.Popen | None = None

    try:
        with LOG.open("ab", buffering=0) as log_handle:
            worker = subprocess.Popen(
                [
                    sys.executable,
                    str(WORKER_MAIN),
                    "--database",
                    str(DATABASE),
                    "--state-root",
                    str(STATE_ROOT),
                    "--prewarm-config",
                    str(CONFIG),
                    "--lock-file",
                    str(LOCK),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        first = wait_first_ready(store, worker)
        failed_runtime_id = str(first["runtime_id"])
        failed_pid = int(first["vmm_pid"])
        first_status = runtime.status(failed_runtime_id)
        require(first_status is not None and first_status.get("processAlive") is True, "first VMM is not alive")

        os.kill(failed_pid, signal.SIGKILL)
        replacement = wait_replacement(store, worker, failed_runtime_id)
        replacement_runtime_id = str(replacement["runtime_id"])
        replacement_status = runtime.status(replacement_runtime_id)
        require(replacement_status is not None, "replacement metadata is missing")
        require(replacement_status.get("processAlive") is True, "replacement VMM is not alive")
        require(runtime.network_manager.store.get(failed_runtime_id) is None, "failed lease remains")
        require(not placeholder(failed_runtime_id).exists(), "failed placeholder remains")
        require(not (JAILER_ROOT / failed_runtime_id).exists(), "failed jail remains")

        print("PASS: Worker detected a dead ready Firecracker and replenished the pool")
        print(
            "PREWARM_WORKER_HEALTH_READY "
            f"failed={failed_runtime_id} failed_state=retired replacement={replacement_runtime_id} "
            "replacement_state=ready network=clean placeholder=clean jail=clean"
        )
    finally:
        if worker is not None and worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=70)
            except subprocess.TimeoutExpired:
                print("CLEANUP_WARNING Worker did not stop safely", file=sys.stderr)
        for runtime_id in all_runtime_ids(store):
            cleanup_runtime(runtime, store, runtime_id)
        LOCK.unlink(missing_ok=True)
        shutil.rmtree(STATE_ROOT, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FAIL: prewarm Worker health smoke test: {error}", file=sys.stderr)
        raise SystemExit(1)
