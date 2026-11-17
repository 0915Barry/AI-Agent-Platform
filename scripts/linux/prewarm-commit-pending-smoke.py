#!/usr/bin/env python3
"""验证 Worker --once 对 ``commit_pending`` 领取执行向前修复。

场景：领取已提交到「binding=running」，只差 item 未置 assigned。Worker 应只补
两笔记账（item→assigned、op→succeeded），不停 VMM、不释放附件、不删 binding。
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_MANAGER = REPO_ROOT / "services" / "instance-manager"
sys.path.insert(0, str(INSTANCE_MANAGER))

from prewarm_store import PrewarmStore  # noqa: E402
from runtime import InstanceRuntime  # noqa: E402
from runtime_store import RuntimeStore, VolumeAttachmentStore  # noqa: E402
from startup_store import StartupStore  # noqa: E402


STATE_ROOT = Path("/var/lib/fc/prewarm-commit-pending-smoke")
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
        if item is not None:
            status = str(item["status"])
            if status == "preparing":
                store.mark_prepare_error(runtime_id, "smoke cleanup")
            elif status == "claiming":
                operation_id = item.get("operation_id") or ""
                if operation_id:
                    try:
                        store.mark_error(runtime_id, operation_id, "smoke cleanup")
                    except Exception:
                        pass
            item = store.get(runtime_id)
            if item is not None and str(item["status"]) in {"ready", "error"}:
                store.mark_draining(runtime_id)
        status = runtime.status(runtime_id)
        if status is not None and status.get("processAlive") is True:
            runtime.stop(runtime_id, reason="prewarm_commit_pending_smoke_cleanup")
        runtime.destroy(runtime_id, preserve_volume=True)
        item = store.get(runtime_id)
        if item is not None and str(item["status"]) in {"draining", "error"}:
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
        "PREWARM_CLAIM_TIMEOUT_SECONDS=30\n"
        "PREWARM_ACTIVATE_TIMEOUT_SECONDS=10\n"
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
    startup_store = StartupStore(DATABASE)
    attachment_store = VolumeAttachmentStore(DATABASE)
    runtime_store = RuntimeStore(DATABASE)

    runtime_id = f"prewarm-commit-{uuid.uuid4().hex[:12]}"
    logical_id = f"agent-commit-{uuid.uuid4().hex[:12]}"
    operation_id = f"op-{uuid.uuid4().hex[:12]}"
    activation_id = f"act-{uuid.uuid4().hex[:12]}"
    volume_path = f"{VOLUME_ROOT}/{logical_id}.ext4"
    token = f"commit-{uuid.uuid4().hex}"
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    try:
        store.create_item(runtime_id, PROFILE_KEY, claim_token_hash=token_hash)
        warm = runtime.prepare_prewarmer(runtime_id, token, min_free_network_slots=0)
        require(
            store.record_runtime_generation(
                runtime_id,
                int(warm["pid"]),
                int(warm["processStartTicks"]),
            ),
            "failed to record prewarm runtime generation",
        )
        require(store.mark_ready(runtime_id, ttl_seconds=300), "failed to mark ready")

        startup_store.begin(operation_id, logical_id, "user-commit", "prewarm")
        claimed = store.claim(
            PROFILE_KEY,
            logical_id,
            "user-commit",
            volume_path,
            operation_id,
            activation_id,
        )
        require(claimed is not None, "failed to claim ready prewarm item")
        # 模拟领取已走到「binding=running、op=committing」，只差 item 未 assigned。
        attachment_store.reserve(
            volume_path,
            "user-commit",
            logical_id,
            runtime_id,
            operation_id,
        )
        attachment_store.mark_attached(volume_path, operation_id)
        runtime_store.save_binding(
            {
                "logicalInstanceId": logical_id,
                "runtimeId": runtime_id,
                "userId": "user-commit",
                "volumePath": volume_path,
                "activationId": activation_id,
                "status": "running",
            }
        )
        startup_store.transition(operation_id, "deciding", "executing")
        require(
            startup_store.transition(
                operation_id, "executing", "committing", runtime_id=runtime_id
            ),
            "failed to transition startup operation to committing",
        )

        stale_at = int(time.time()) - 120
        with sqlite3.connect(DATABASE) as connection:
            connection.execute(
                "UPDATE prewarm_items SET claimed_at=?, updated_at=? WHERE runtime_id=?",
                (stale_at, stale_at, runtime_id),
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
            capture_output=True,
            text=True,
        )
        output = completed.stdout + completed.stderr
        require(completed.returncode == 0, f"Worker --once failed:\n{output}")

        operation = startup_store.get(operation_id)
        require(operation is not None and operation["status"] == "succeeded", "startup operation was not succeeded")
        item = store.get(runtime_id)
        require(item is not None and item["status"] == "assigned", "prewarm item was not assigned")
        require(attachment_store.get(volume_path) is not None, "attachment should remain attached")
        require(runtime_store.get_binding(logical_id) is not None, "binding should remain running")
        # 关键：向前修复不停 VMM，预热机必须仍然存活。
        status = runtime.status(runtime_id)
        require(status is not None and status.get("processAlive") is True, "VMM was stopped unexpectedly")
        require(
            "PREWARM_WORKER_CLAIM_RECOVERY compensated=1" in output,
            "claim recovery log marker missing",
        )

        print("PASS: Worker repaired a stale commit claim forward without touching the runtime")
        print(
            "PREWARM_COMMIT_PENDING_READY "
            f"runtime={runtime_id} state=assigned operation=succeeded vmm=alive "
            "attachment=kept binding=kept"
        )
    finally:
        for ready in store.list_ready(PROFILE_KEY):
            cleanup_runtime(runtime, store, str(ready["runtime_id"]))
        cleanup_runtime(runtime, store, runtime_id)
        shutil.rmtree(STATE_ROOT, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FAIL: prewarm commit pending smoke test: {error}", file=sys.stderr)
        raise SystemExit(1)
