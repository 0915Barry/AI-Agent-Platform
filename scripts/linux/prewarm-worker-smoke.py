#!/usr/bin/env python3
"""验证 Worker --once 能在真实 Linux Host 自动创建并清理一台预热机。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_MANAGER = REPO_ROOT / "services" / "instance-manager"
sys.path.insert(0, str(INSTANCE_MANAGER))

from prewarm_store import PrewarmStore  # noqa: E402
from runtime import InstanceRuntime  # noqa: E402


STATE_ROOT = Path("/var/lib/fc/prewarm-worker-smoke")
DATABASE = STATE_ROOT / "control-plane.db"
CONFIG = STATE_ROOT / "prewarm.env"
PROFILE_KEY = "default-2vcpu-1g"
VOLUME_ROOT = Path("/srv/fc/volumes/users")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> None:
    if STATE_ROOT.exists():
        raise RuntimeError(f"test state already exists; inspect it first: {STATE_ROOT}")

    STATE_ROOT.mkdir(parents=True, mode=0o755)
    CONFIG.write_text(
        "PREWARM_ENABLED=1\n"
        "PREWARM_TARGET_READY=1\n"
        "PREWARM_MAX_TOTAL=1\n"
        "PREWARM_BUILD_CONCURRENCY=1\n"
        "PREWARM_TTL_SECONDS=300\n"
        "PREWARM_HEALTH_INTERVAL_SECONDS=5\n"
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
    runtime_id: str | None = None
    placeholder: Path | None = None

    try:
        command = [
            sys.executable,
            str(INSTANCE_MANAGER / "prewarm_worker_main.py"),
            "--database",
            str(DATABASE),
            "--state-root",
            str(STATE_ROOT),
            "--prewarm-config",
            str(CONFIG),
            "--profile-key",
            PROFILE_KEY,
            "--once",
        ]
        completed = subprocess.run(command, check=False)
        require(completed.returncode == 0, "prewarm Worker --once failed")

        ready_items = store.list_ready(PROFILE_KEY)
        require(len(ready_items) == 1, "Worker did not publish exactly one ready item")
        item = ready_items[0]
        runtime_id = str(item["runtime_id"])
        placeholder = VOLUME_ROOT.parent / "prewarm" / f"{runtime_id}.placeholder.ext4"
        require(item.get("vmm_pid") is not None, "ready item has no VMM PID")
        require(item.get("process_start_ticks") is not None, "ready item has no process generation")
        require(len(str(item.get("claim_token_hash") or "")) == 64, "claim token hash is invalid")

        status = runtime.status(runtime_id)
        require(status is not None, "prepared runtime metadata is missing")
        require(status.get("status") == "running", "prepared runtime is not running")
        require(status.get("processAlive") is True, "prepared Firecracker process is not alive")
        require(placeholder is not None and placeholder.is_file(), "placeholder disk is missing")
        require(runtime.network_manager.store.get(runtime_id) is not None, "network lease is missing")

        require(store.mark_draining(runtime_id), "ready item did not enter draining")
        stopped = runtime.stop(runtime_id, reason="prewarm_worker_smoke")
        require(stopped.get("status") == "stopped", "prewarm runtime did not stop")
        runtime.destroy(runtime_id, preserve_volume=True)
        require(store.retire(runtime_id), "drained item did not retire")

        require(runtime.network_manager.store.get(runtime_id) is None, "network lease remains")
        require(not placeholder.exists(), "placeholder disk remains")
        require(
            not (Path("/srv/jailer/firecracker") / runtime_id).exists(),
            "Jailer directory remains",
        )
        print("PASS: Worker --once automatically prepared and cleaned a real prewarmed microVM")
        print(
            "PREWARM_WORKER_SMOKE_READY "
            f"runtime={runtime_id} state=retired generation=verified "
            "network=clean placeholder=clean jail=clean"
        )
    finally:
        if runtime_id is None:
            ready_items = store.list_ready(PROFILE_KEY)
            if ready_items:
                runtime_id = str(ready_items[0]["runtime_id"])
                placeholder = VOLUME_ROOT.parent / "prewarm" / f"{runtime_id}.placeholder.ext4"
        if runtime_id is not None:
            try:
                item = store.get(runtime_id)
                if item is not None and item.get("status") in {"ready", "error"}:
                    store.mark_draining(runtime_id)
                status = runtime.status(runtime_id)
                if status is not None and status.get("status") == "running":
                    runtime.stop(runtime_id, reason="prewarm_worker_smoke_cleanup")
                runtime.destroy(runtime_id, preserve_volume=True)
                item = store.get(runtime_id)
                if item is not None and item.get("status") in {"draining", "error"}:
                    store.retire(runtime_id)
            except Exception as error:
                print(f"CLEANUP_WARNING managed cleanup failed: {error}", file=sys.stderr)
            try:
                runtime.network_manager.release(runtime_id)
            except Exception:
                pass
            if placeholder is not None:
                placeholder.unlink(missing_ok=True)
            shutil.rmtree(Path("/srv/jailer/firecracker") / runtime_id, ignore_errors=True)
        shutil.rmtree(STATE_ROOT, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FAIL: prewarm Worker smoke test: {error}", file=sys.stderr)
        raise SystemExit(1)
