#!/usr/bin/env python3
"""通过真实 Control Plane HTTP API 验证单台预热机的完整接管与退出。"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import shutil
import socket
import sqlite3
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


STATE_ROOT = Path("/var/lib/fc/prewarm-control-plane-smoke")
DATABASE = STATE_ROOT / "control-plane.db"
VOLUME_ROOT = Path("/srv/fc/volumes/users")
PORT = 18096
BASE_URL = f"http://127.0.0.1:{PORT}"
PROFILE_KEY = "default-2vcpu-1g"


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
    deadline = time.monotonic() + 10
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


def pi_get_state(host: str, port: int) -> None:
    request_id = "prewarm-control-plane-state"
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
                    return


def main() -> None:
    if STATE_ROOT.exists():
        raise RuntimeError(f"test state already exists; inspect it first: {STATE_ROOT}")

    suffix = uuid.uuid4().hex[:10]
    runtime_id = f"prewarm-cp-{suffix}"
    logical_id = f"prewarm-user-{suffix}"
    username = f"prewarm-{suffix}"
    password = f"Smoke-{suffix}-{uuid.uuid4().hex[:12]}"
    operation_token = f"claim-{uuid.uuid4().hex}"
    token_hash = hashlib.sha256(operation_token.encode()).hexdigest()
    credential = STATE_ROOT / "placeholder-api-key"
    config_path = STATE_ROOT / "prewarm.env"
    log_path = STATE_ROOT / "server.log"
    server: subprocess.Popen | None = None
    user_id = ""
    volume_path: Path | None = None

    STATE_ROOT.mkdir(parents=True, mode=0o755)
    credential.write_text("prewarm-control-plane-placeholder\n", encoding="utf-8")
    credential.chmod(0o600)
    config_path.write_text(
        "PREWARM_ENABLED=1\n"
        "PREWARM_TARGET_READY=1\n"
        "PREWARM_MAX_TOTAL=2\n"
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
        prewarm_store.create_item(
            runtime_id,
            PROFILE_KEY,
            host_id=socket.gethostname(),
            claim_token_hash=token_hash,
        )
        warm = runtime.prepare_prewarmer(
            runtime_id,
            operation_token,
            min_free_network_slots=0,
        )
        require(
            prewarm_store.record_runtime_generation(
                runtime_id,
                int(warm["pid"]),
                int(warm["processStartTicks"]),
            ),
            "failed to record prewarm generation",
        )
        require(
            prewarm_store.mark_ready(
                runtime_id,
                ttl_seconds=300,
                claim_token_hash=token_hash,
            ),
            "failed to mark prewarm item ready",
        )

        log_handle = log_path.open("ab", buffering=0)
        server = subprocess.Popen(
            [
                sys.executable,
                str(CONTROL_PLANE / "server.py"),
                "--listen-host",
                "127.0.0.1",
                "--listen-port",
                str(PORT),
                "--database",
                str(DATABASE),
                "--state-root",
                str(STATE_ROOT),
                "--deepseek-credential",
                str(credential),
                "--prewarm-config",
                str(config_path),
                "--require-auth",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_handle.close()
        wait_for_control_plane(server, log_path)

        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        request_json(
            opener,
            "POST",
            "/api/auth/login",
            {"username": username, "password": password},
        )
        created = request_json(opener, "POST", "/api/instances", {"id": logical_id})
        require(created.get("status") == "created", "logical instance was not created")
        started = request_json(opener, "POST", f"/api/instances/{logical_id}/start")
        runtime_payload = started.get("runtime") or {}
        require(started.get("status") == "running", "Control Plane did not mark running")
        require(runtime_payload.get("id") == runtime_id, "Control Plane did not claim prewarmer")
        pi_get_state(str(runtime_payload["guestIp"]), int(runtime_payload["rpcPort"]))

        with sqlite3.connect(DATABASE) as connection:
            operation = connection.execute(
                "SELECT strategy,status,runtime_id FROM startup_operations "
                "WHERE logical_instance_id=? ORDER BY created_at DESC LIMIT 1",
                (logical_id,),
            ).fetchone()
        require(operation == ("prewarm", "succeeded", runtime_id), "startup audit mismatch")
        require(prewarm_store.get(runtime_id)["status"] == "assigned", "item not assigned")
        require(runtime_store.get_binding(logical_id) is not None, "runtime binding missing")
        require(attachment_store.get(str(volume_path)) is not None, "attachment missing")

        stopped = request_json(opener, "POST", f"/api/instances/{logical_id}/stop")
        require(stopped.get("status") == "stopped", "Control Plane did not stop instance")
        require(prewarm_store.get(runtime_id)["status"] == "retired", "item not retired")
        require(runtime_store.get_binding(logical_id) is None, "binding was not deleted")
        require(attachment_store.get(str(volume_path)) is None, "attachment was not released")
        require(runtime_store.get(runtime_id) is None, "physical runtime metadata remains")
        require(runtime.network_manager.store.get(runtime_id) is None, "network lease remains")
        require(volume_path.is_file(), "user volume was not preserved")

        destroyed = request_json(opener, "DELETE", f"/api/instances/{logical_id}")
        require(destroyed.get("status") == "destroyed", "logical instance was not destroyed")
        print("PASS: Control Plane claimed and retired a real prewarmed microVM")
        print(
            "PREWARM_CONTROL_PLANE_READY "
            f"runtime={runtime_id} logical={logical_id} http=authenticated strategy=prewarm "
            "rpc=verified stop=retired binding=clean attachment=clean network=clean volume=preserved"
        )
    except Exception:
        if log_path.exists():
            print("--- temporary Control Plane log ---", file=sys.stderr)
            print(
                log_path.read_text(encoding="utf-8", errors="replace")[-12000:],
                file=sys.stderr,
            )
        raise
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=2)
        try:
            binding = runtime_store.get_binding(logical_id) if logical_id else None
            target = logical_id if binding is not None else runtime_id
            status = runtime.status(target)
            if status is not None and status.get("status") == "running":
                runtime.stop(target, reason="prewarm_cp_smoke_cleanup")
            if binding is not None:
                try:
                    manager.complete_logical_stop(logical_id)
                except Exception:
                    pass
            if runtime_store.get(runtime_id) is not None:
                runtime.destroy(runtime_id, preserve_volume=True)
        except Exception as error:
            print(f"CLEANUP_WARNING {error}", file=sys.stderr)
        try:
            runtime.network_manager.release(runtime_id)
        except Exception:
            pass
        if volume_path is not None:
            volume_path.unlink(missing_ok=True)
        (VOLUME_ROOT.parent / "prewarm" / f"{runtime_id}.placeholder.ext4").unlink(
            missing_ok=True
        )
        shutil.rmtree(Path("/srv/jailer/firecracker") / runtime_id, ignore_errors=True)
        shutil.rmtree(STATE_ROOT, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FAIL: prewarm Control Plane smoke test: {error}", file=sys.stderr)
        raise SystemExit(1)
