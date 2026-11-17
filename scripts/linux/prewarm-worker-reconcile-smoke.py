#!/usr/bin/env python3
"""验证 Worker 重启协调会清理遗留 preparing，并自动补回 ready。"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_MANAGER = REPO_ROOT / "services" / "instance-manager"
sys.path.insert(0, str(INSTANCE_MANAGER))

from prewarm_store import PrewarmStore  # noqa: E402
from runtime import InstanceRuntime  # noqa: E402


STATE_ROOT = Path("/var/lib/fc/prewarm-worker-reconcile-smoke")
DATABASE = STATE_ROOT / "control-plane.db"
CONFIG = STATE_ROOT / "prewarm.env"
PROFILE_KEY = "default-2vcpu-1g"
VOLUME_ROOT = Path("/srv/fc/volumes/users")
JAILER_ROOT = Path("/srv/jailer/firecracker")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def placeholder_path(runtime_id: str) -> Path:
    return VOLUME_ROOT.parent / "prewarm" / f"{runtime_id}.placeholder.ext4"


def cleanup_runtime(runtime: InstanceRuntime, store: PrewarmStore, runtime_id: str) -> None:
    try:
        item = store.get(runtime_id)
        if item is not None and item.get("status") == "preparing":
            store.mark_prepare_error(runtime_id, "smoke cleanup")
            item = store.get(runtime_id)
        if item is not None and item.get("status") in {"ready", "error"}:
            store.mark_draining(runtime_id)
        status = runtime.status(runtime_id)
        if status is not None and status.get("processAlive") is True:
            runtime.stop(runtime_id, reason="prewarm_reconcile_smoke_cleanup")
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
    placeholder_path(runtime_id).unlink(missing_ok=True)
    shutil.rmtree(JAILER_ROOT / runtime_id, ignore_errors=True)


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
    stale_runtime_id = f"prewarm-stale-{uuid.uuid4().hex[:12]}"
    replacement_runtime_id: str | None = None
    token = f"reconcile-{uuid.uuid4().hex}"
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    try:
        store.create_item(
            stale_runtime_id,
            PROFILE_KEY,
            claim_token_hash=token_hash,
        )
        warm = runtime.prepare_prewarmer(
            stale_runtime_id,
            token,
            min_free_network_slots=0,
        )
        require(
            store.record_runtime_generation(
                stale_runtime_id,
                int(warm["pid"]),
                int(warm["processStartTicks"]),
            ),
            "failed to record stale prewarm generation",
        )
        require(
            store.get(stale_runtime_id)["status"] == "preparing",
            "failed to create interrupted preparing state",
        )

        completed = subprocess.run(
            [
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
            ],
            check=False,
        )
        require(completed.returncode == 0, "reconciling Worker --once failed")

        stale_item = store.get(stale_runtime_id)
        require(stale_item is not None, "stale prewarm record disappeared")
        require(stale_item["status"] == "retired", "stale preparing item was not retired")
        require(runtime.network_manager.store.get(stale_runtime_id) is None, "stale lease remains")
        require(not placeholder_path(stale_runtime_id).exists(), "stale placeholder remains")
        require(not (JAILER_ROOT / stale_runtime_id).exists(), "stale jail remains")

        ready_items = store.list_ready(PROFILE_KEY)
        require(len(ready_items) == 1, "Worker did not create one replacement ready item")
        replacement_runtime_id = str(ready_items[0]["runtime_id"])
        require(replacement_runtime_id != stale_runtime_id, "stale runtime was reused")
        replacement = runtime.status(replacement_runtime_id)
        require(replacement is not None, "replacement runtime metadata is missing")
        require(replacement.get("processAlive") is True, "replacement VMM is not alive")
        require(
            int(replacement["pid"]) == int(ready_items[0]["vmm_pid"])
            and int(replacement["processStartTicks"])
            == int(ready_items[0]["process_start_ticks"]),
            "replacement generation does not match SQLite",
        )

        print("PASS: Worker reconciled an interrupted prewarm and replenished the pool")
        print(
            "PREWARM_WORKER_RECONCILE_READY "
            f"stale={stale_runtime_id} stale_state=retired replacement={replacement_runtime_id} "
            "replacement_state=ready stale_network=clean stale_placeholder=clean stale_jail=clean"
        )
    finally:
        if replacement_runtime_id is None:
            ready_items = store.list_ready(PROFILE_KEY)
            if ready_items:
                replacement_runtime_id = str(ready_items[0]["runtime_id"])
        if replacement_runtime_id is not None:
            cleanup_runtime(runtime, store, replacement_runtime_id)
        cleanup_runtime(runtime, store, stale_runtime_id)
        shutil.rmtree(STATE_ROOT, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FAIL: prewarm Worker reconcile smoke test: {error}", file=sys.stderr)
        raise SystemExit(1)
