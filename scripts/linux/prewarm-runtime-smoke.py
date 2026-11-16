#!/usr/bin/env python3
"""真实串联预热 Runtime、状态存储和领取编排，不经过 Control Plane。"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_MANAGER = REPO_ROOT / "services" / "instance-manager"
sys.path.insert(0, str(INSTANCE_MANAGER))

from prewarm_config import PrewarmConfig  # noqa: E402
from prewarm_manager import PrewarmManager  # noqa: E402
from prewarm_store import PrewarmStore  # noqa: E402
from runtime import InstanceRuntime  # noqa: E402
from runtime_store import RuntimeStore, VolumeAttachmentStore  # noqa: E402
from startup_store import StartupStore  # noqa: E402


STATE_ROOT = Path("/var/lib/fc/prewarm-runtime-smoke")
DATABASE = STATE_ROOT / "control-plane.db"
VOLUME_ROOT = Path("/srv/fc/volumes/users")
PROFILE_KEY = "default-2vcpu-1g"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def pi_get_state(host: str, port: int) -> None:
    """确认激活后的 Pi RPC 端点可以处理一条真实 JSONL 命令。"""
    request_id = "prewarm-runtime-state"
    with socket.create_connection((host, port), timeout=5) as connection:
        connection.settimeout(15)
        connection.sendall(
            (json.dumps({"id": request_id, "type": "get_state"}) + "\n").encode()
        )
        buffer = b""
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                raise RuntimeError("Pi RPC closed before get_state response")
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                if not raw:
                    continue
                event = json.loads(raw)
                if event.get("id") == request_id and event.get("type") == "response":
                    require(event.get("success") is True, "Pi RPC get_state failed")
                    require(event.get("command") == "get_state", "unexpected Pi RPC response")
                    return


def main() -> None:
    if STATE_ROOT.exists():
        raise RuntimeError(f"test state already exists; clean it first: {STATE_ROOT}")

    suffix = uuid.uuid4().hex[:10]
    runtime_id = f"prewarm-runtime-{suffix}"
    logical_id = f"prewarm-user-{suffix}"
    user_id = f"prewarm-owner-{suffix}"
    operation_id = f"operation-{suffix}"
    activation_id = f"activation-{suffix}"
    token = f"claim-{uuid.uuid4().hex}"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    volume_path = VOLUME_ROOT / f"{user_id}.ext4"
    placeholder_path = VOLUME_ROOT.parent / "prewarm" / f"{runtime_id}.placeholder.ext4"
    credential_path = STATE_ROOT / "placeholder-api-key"

    STATE_ROOT.mkdir(parents=True, mode=0o755)
    credential_path.write_text("prewarm-runtime-placeholder\n", encoding="utf-8")
    credential_path.chmod(0o600)

    runtime = InstanceRuntime(
        state_root=STATE_ROOT,
        volume_root=VOLUME_ROOT,
        state_database=DATABASE,
        snapshot_root=STATE_ROOT / "snapshots",
        deepseek_credential=credential_path,
    )
    prewarm_store = PrewarmStore(DATABASE)
    startup_store = StartupStore(DATABASE)
    attachment_store = VolumeAttachmentStore(DATABASE)
    runtime_store = RuntimeStore(DATABASE)
    manager = PrewarmManager(
        PrewarmConfig.from_env(
            {
                "PREWARM_ENABLED": "1",
                "PREWARM_TARGET_READY": "1",
                "PREWARM_MAX_TOTAL": "1",
                "PREWARM_MIN_FREE_NETWORK_SLOTS": "0",
            }
        ),
        prewarm_store=prewarm_store,
        attachment_store=attachment_store,
        runtime_store=runtime_store,
        activator=runtime,
        new_activation_id=lambda: activation_id,
    )

    stopped = False
    completed_stop = False
    try:
        prewarm_store.create_item(
            runtime_id,
            PROFILE_KEY,
            host_id=socket.gethostname(),
            claim_token_hash=token_hash,
        )
        warm = runtime.prepare_prewarmer(runtime_id, token, min_free_network_slots=0)
        require(warm.get("status") == "running", "prewarm runtime did not start")
        require(warm.get("processAlive") is True, "prewarm VMM is not alive")
        require(
            prewarm_store.record_runtime_generation(
                runtime_id,
                int(warm["pid"]),
                int(warm["processStartTicks"]),
            ),
            "failed to record prewarm VMM generation",
        )
        require(
            prewarm_store.mark_ready(
                runtime_id,
                ttl_seconds=300,
                claim_token_hash=token_hash,
            ),
            "failed to mark prewarm runtime ready",
        )

        startup_store.begin(operation_id, logical_id, user_id, "prewarm")
        require(
            startup_store.transition(operation_id, "deciding", "executing"),
            "failed to enter executing startup state",
        )
        claim = manager.claim(
            PROFILE_KEY,
            logical_id,
            user_id,
            str(volume_path),
            operation_id,
        )
        require(claim.claimed, f"prewarm claim failed: {claim.reason}")
        require(claim.runtime_id == runtime_id, "claimed an unexpected runtime")
        require(
            startup_store.transition(
                operation_id,
                "executing",
                "committing",
                runtime_id=runtime_id,
            ),
            "failed to enter committing startup state",
        )
        manager.commit(logical_id, runtime_id, operation_id, activation_id)
        require(startup_store.mark_succeeded(operation_id), "failed to commit startup operation")

        item = prewarm_store.get(runtime_id)
        binding = runtime_store.get_binding(logical_id)
        attachment = attachment_store.get(str(volume_path))
        require(item is not None and item["status"] == "assigned", "item is not assigned")
        require(binding is not None and binding["status"] == "running", "binding is not running")
        require(
            attachment is not None and attachment["status"] == "attached",
            "user volume is not attached",
        )
        pi_get_state(str(claim.runtime["guestIp"]), int(claim.runtime["rpcPort"]))

        stopped_state = runtime.stop(logical_id, reason="prewarm_runtime_smoke")
        stopped = True
        require(stopped_state.get("status") == "stopped", "runtime did not stop")
        require(volume_path.is_file(), "user volume was not preserved after stop")
        manager.complete_stop(logical_id, runtime_id, operation_id)
        completed_stop = True

        require(runtime_store.get_binding(logical_id) is None, "runtime binding was not removed")
        require(attachment_store.get(str(volume_path)) is None, "volume attachment was not released")
        retired = prewarm_store.get(runtime_id)
        require(retired is not None and retired["status"] == "retired", "item was not retired")
        require(runtime.network_manager.store.get(runtime_id) is None, "network lease was not released")

        print("PASS: Runtime completed a real prewarm prepare, claim, activation, commit, and stop")
        print(
            "PREWARM_RUNTIME_READY "
            f"runtime={runtime_id} logical={logical_id} state=retired "
            "rpc=verified volume=preserved binding=clean attachment=clean network=clean"
        )
    finally:
        # 失败路径也只清理本次随机 ID 对应的资源，用户正式数据不在测试范围内。
        try:
            if not stopped:
                target = logical_id if runtime_store.get_binding(logical_id) else runtime_id
                if runtime.status(target) is not None:
                    runtime.stop(target, reason="prewarm_runtime_smoke_cleanup")
                    stopped = True
            if stopped and not completed_stop:
                item = prewarm_store.get(runtime_id)
                if item is not None and item.get("status") == "assigned":
                    manager.complete_stop(logical_id, runtime_id, operation_id)
                    completed_stop = True
        except Exception as error:
            print(f"CLEANUP_WARNING managed cleanup failed: {error}", file=sys.stderr)
        try:
            runtime.destroy(runtime_id, preserve_volume=True)
        except Exception as error:
            print(f"CLEANUP_WARNING runtime destroy failed: {error}", file=sys.stderr)
        try:
            runtime.network_manager.release(runtime_id)
        except Exception:
            pass
        volume_path.unlink(missing_ok=True)
        placeholder_path.unlink(missing_ok=True)
        shutil.rmtree(Path("/srv/jailer/firecracker") / runtime_id, ignore_errors=True)
        shutil.rmtree(STATE_ROOT, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FAIL: prewarm Runtime smoke test: {error}", file=sys.stderr)
        raise SystemExit(1)
