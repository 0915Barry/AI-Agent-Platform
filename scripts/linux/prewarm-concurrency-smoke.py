#!/usr/bin/env python3
"""真实预热池并发正确性验收：场景 A（有限池争抢）。

先由 Worker 准备固定数量的 ready Guest，然后停止 Worker 并同时释放
多个独立用户的 START。验证 ready 项不重复领取、池空立即冷启动、
数据盘不混用，且并发 STOP 后不遗留运行资源。
"""

from __future__ import annotations

import concurrent.futures
import http.cookiejar
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_MANAGER = REPO_ROOT / "services" / "instance-manager"
CONTROL_PLANE = REPO_ROOT / "services" / "control-plane"
sys.path[:0] = [str(INSTANCE_MANAGER), str(CONTROL_PLANE)]

from auth import AuthStore  # noqa: E402
from prewarm_store import PrewarmStore  # noqa: E402
from runtime import InstanceRuntime  # noqa: E402


STATE_ROOT = Path("/var/lib/fc/prewarm-concurrency")
DATABASE = STATE_ROOT / "control-plane.db"
RESULT_ROOT = Path("/var/lib/fc/prewarm-concurrency-results")
VOLUME_ROOT = Path("/srv/fc/volumes/users")
JAILER_ROOT = Path("/srv/jailer/firecracker")
PLACEHOLDER_DIR = VOLUME_ROOT.parent / "prewarm"
PORT = 18099
BASE_URL = f"http://127.0.0.1:{PORT}"
PROFILE_KEY = "default-2vcpu-1g"
WORKER_LOCK = Path("/run/ai-agent-prewarm-concurrency.lock")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class HttpRequestError(RuntimeError):
    def __init__(self, status: int, code: str, route: str, detail: str) -> None:
        super().__init__(f"HTTP {status}/{code} {route}: {detail}")
        self.status = status
        self.code = code


def request_json(opener, method: str, route: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        BASE_URL + route,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener.open(request, timeout=180) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        error_code = "http_error"
        try:
            error_payload = json.loads(detail).get("error") or {}
            if isinstance(error_payload.get("code"), str):
                error_code = error_payload["code"]
        except (json.JSONDecodeError, AttributeError):
            pass
        raise HttpRequestError(error.code, error_code, route, detail) from error


def authenticated_opener(username: str, password: str):
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    request_json(
        opener,
        "POST",
        "/api/auth/login",
        {"username": username, "password": password},
    )
    return opener


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


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def start_worker(config_path: Path, log_path: Path) -> subprocess.Popen:
    handle = log_path.open("ab", buffering=0)
    process = subprocess.Popen(
        [
            sys.executable,
            str(INSTANCE_MANAGER / "prewarm_worker_main.py"),
            "--database",
            str(DATABASE),
            "--state-root",
            str(STATE_ROOT),
            "--prewarm-config",
            str(config_path),
            "--profile-key",
            PROFILE_KEY,
            "--lock-file",
            str(WORKER_LOCK),
            "--max-backoff",
            "5",
        ],
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    handle.close()
    return process


def wait_for_ready_exact(store: PrewarmStore, count: int, timeout: float = 300) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = store.list_ready(PROFILE_KEY)
        counts = store.capacity_counts(PROFILE_KEY)
        if (
            len(ready) == count
            and counts["preparing"] == 0
            and counts["claiming"] == 0
        ):
            return ready
        time.sleep(0.5)
    raise RuntimeError(f"prewarm pool did not reach exactly {count} ready items")


def query_rows(table: str) -> list[dict]:
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
    finally:
        connection.close()


def concurrent_requests(
    cases: list[dict],
    method: str,
    route_suffix: str,
    *,
    capture_http_errors: bool = False,
) -> list[dict]:
    """使所有工作线程在同一 Barrier 后同时发送 HTTP 请求。"""
    barrier = threading.Barrier(len(cases) + 1)

    def invoke(case: dict) -> dict:
        barrier.wait(timeout=15)
        started = time.monotonic()
        try:
            response = request_json(
                case["opener"], method, f"/api/instances/{case['instanceId']}{route_suffix}"
            )
            return {
                "userId": case["userId"],
                "instanceId": case["instanceId"],
                "latencyMs": round((time.monotonic() - started) * 1000),
                "httpStatus": 200,
                "errorCode": None,
                "response": response,
            }
        except HttpRequestError as error:
            if not capture_http_errors:
                raise
            return {
                "userId": case["userId"],
                "instanceId": case["instanceId"],
                "latencyMs": round((time.monotonic() - started) * 1000),
                "httpStatus": error.status,
                "errorCode": error.code,
                "response": None,
            }

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(cases)) as executor:
        futures = [executor.submit(invoke, case) for case in cases]
        barrier.wait(timeout=15)
        return [future.result(timeout=210) for future in futures]


def cleanup_runtime(runtime: InstanceRuntime, runtime_id: str) -> None:
    try:
        status = runtime.status(runtime_id)
        if status is not None and status.get("processAlive") is True:
            runtime.stop(runtime_id, reason="prewarm_concurrency_cleanup")
        runtime.destroy(runtime_id, preserve_volume=True)
    except Exception as error:
        print(f"CLEANUP_WARNING runtime={runtime_id} error={error}", file=sys.stderr)
    try:
        runtime.network_manager.release(runtime_id)
    except Exception:
        pass
    (PLACEHOLDER_DIR / f"{runtime_id}.placeholder.ext4").unlink(missing_ok=True)
    shutil.rmtree(JAILER_ROOT / runtime_id, ignore_errors=True)


def archive_failure(error: Exception, results: list[dict], server_log: Path, worker_log: Path) -> Path:
    result_dir = RESULT_ROOT / (
        "failed-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    )
    result_dir.mkdir(parents=True, mode=0o755, exist_ok=False)
    (result_dir / "failure.json").write_text(
        json.dumps(
            {
                "error": str(error),
                "traceback": traceback.format_exc(),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if DATABASE.is_file():
        source = sqlite3.connect(DATABASE, timeout=10)
        target = sqlite3.connect(result_dir / "control-plane.db")
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
    for name, path in (("control-plane.log", server_log), ("worker.log", worker_log)):
        if path.exists():
            shutil.copy2(path, result_dir / name)
    for name, command in (
        ("process.txt", ["ps", "-eo", "pid,ppid,user,stat,lstart,args"]),
        ("network.txt", ["ip", "-details", "link", "show"]),
        ("nftables.txt", ["nft", "list", "tables"]),
    ):
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        (result_dir / name).write_text(
            (completed.stdout or "") + (completed.stderr or ""), encoding="utf-8"
        )
    return result_dir


def main() -> None:
    users = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    ready_target = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    require(2 <= users <= 10, "users must be between 2 and 10")
    require(1 <= ready_target < users, "ready must be at least 1 and below users")
    require(1 <= rounds <= 20, "rounds must be between 1 and 20")
    if STATE_ROOT.exists():
        raise RuntimeError(f"test state already exists; inspect it first: {STATE_ROOT}")

    suffix = uuid.uuid4().hex[:10]
    credential = STATE_ROOT / "placeholder-api-key"
    config_path = STATE_ROOT / "prewarm.env"
    server_log = STATE_ROOT / "control-plane.log"
    worker_log = STATE_ROOT / "worker.log"
    server: subprocess.Popen | None = None
    worker: subprocess.Popen | None = None
    cases: list[dict] = []
    all_results: list[dict] = []
    identities: list[dict] = []

    STATE_ROOT.mkdir(parents=True, mode=0o755)
    credential.write_text("prewarm-concurrency-placeholder\n", encoding="utf-8")
    credential.chmod(0o600)
    config_path.write_text(
        "PREWARM_ENABLED=1\n"
        f"PREWARM_TARGET_READY={ready_target}\n"
        f"PREWARM_MAX_TOTAL={ready_target}\n"
        f"PREWARM_BUILD_CONCURRENCY={ready_target}\n"
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

    try:
        auth_store = AuthStore(DATABASE)
        for index in range(users):
            username = f"conc-{suffix}-{index}"
            password = f"Concurrent-{suffix}-{index}-{uuid.uuid4().hex[:8]}"
            user = auth_store.create_user(username, password, claim_unowned=False)
            identities.append(
                {
                    "userId": str(user["id"]),
                    "username": username,
                    "password": password,
                }
            )

        server_handle = server_log.open("ab", buffering=0)
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
            stdout=server_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        server_handle.close()
        wait_for_control_plane(server, server_log)

        for index, identity in enumerate(identities):
            opener = authenticated_opener(identity["username"], identity["password"])
            instance_id = f"conc-{suffix}-{index}"
            created = request_json(opener, "POST", "/api/instances", {"id": instance_id})
            require(created.get("status") == "created", f"failed to create {instance_id}")
            cases.append(
                {
                    "userId": identity["userId"],
                    "instanceId": instance_id,
                    "opener": opener,
                    "username": identity["username"],
                    "password": identity["password"],
                    "volumePath": str(runtime.user_volume_path(identity["userId"])),
                }
            )

        for round_number in range(1, rounds + 1):
            worker = start_worker(config_path, worker_log)
            initial_ready = wait_for_ready_exact(prewarm_store, ready_target)
            initial_ready_ids = {str(item["runtime_id"]) for item in initial_ready}
            stop_process(worker)
            worker = None

            started = concurrent_requests(cases, "POST", "/start")
            all_results.extend(started)
            for result in started:
                require(
                    result["response"].get("status") == "running",
                    f"instance did not reach running: {result['instanceId']}",
                )
                timing = (result["response"].get("runtime") or {}).get(
                    "startupOperationTiming"
                ) or {}
                result["strategy"] = timing.get("strategy")
                result["runtimeId"] = (result["response"].get("runtime") or {}).get("id")

            prewarm_results = [result for result in started if result["strategy"] == "prewarm"]
            cold_results = [result for result in started if result["strategy"] == "cold"]
            require(len(prewarm_results) == ready_target, "unexpected prewarm hit count")
            require(len(cold_results) == users - ready_target, "unexpected cold fallback count")
            claimed_ids = {str(result["runtimeId"]) for result in prewarm_results}
            require(len(claimed_ids) == ready_target, "one prewarmer was assigned twice")
            require(claimed_ids == initial_ready_ids, "start claimed an unexpected prewarmer")

            bindings = query_rows("runtime_bindings")
            attachments = query_rows("volume_attachments")
            running_runtime = [
                row for row in query_rows("runtime_instances") if row["status"] == "running"
            ]
            leases = query_rows("network_leases")
            require(len(bindings) == ready_target, "binding count does not match prewarm hits")
            require(len(attachments) == ready_target, "attachment count does not match prewarm hits")
            require(len({row["volume_path"] for row in attachments}) == len(attachments), "volume attached twice")
            require(len(running_runtime) == users, "running Runtime count does not match users")
            require(len({row["pid"] for row in running_runtime}) == users, "Runtime PID duplicated")
            require(len({row["volume_path"] for row in running_runtime}) == users, "Runtime volume duplicated")
            require(len(leases) == users, "network lease count does not match running users")
            require(len({row["subnet"] for row in leases}) == users, "network subnet duplicated")
            require(len({row["tap"] for row in leases}) == users, "TAP name duplicated")
            require(len({row["nft_table"] for row in leases}) == users, "nftables name duplicated")

            runtime_ids = [str(row["instance_id"]) for row in running_runtime]
            stopped = concurrent_requests(cases, "POST", "/stop")
            all_results.extend(stopped)
            require(
                all(result["response"].get("status") == "stopped" for result in stopped),
                "one or more concurrent stops failed",
            )
            require(not query_rows("runtime_bindings"), "binding remained after concurrent stop")
            require(not query_rows("volume_attachments"), "attachment remained after concurrent stop")
            require(not query_rows("network_leases"), "network lease remained after concurrent stop")
            require(
                not [
                    row
                    for row in query_rows("runtime_instances")
                    if row["status"] == "running"
                ],
                "running Runtime metadata remained after concurrent stop",
            )
            active_items = [
                item
                for item in query_rows("prewarm_items")
                if item["status"] in {"claiming", "assigned"}
            ]
            require(not active_items, "active prewarm item remained after concurrent stop")
            for runtime_id in runtime_ids:
                require(not (JAILER_ROOT / runtime_id).exists(), f"jail remained: {runtime_id}")
                require(
                    not (PLACEHOLDER_DIR / f"{runtime_id}.placeholder.ext4").exists(),
                    f"placeholder remained: {runtime_id}",
                )
            for lease in leases:
                tap_check = subprocess.run(
                    ["ip", "link", "show", str(lease["tap"])],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                require(tap_check.returncode != 0, f"TAP remained: {lease['tap']}")
                nft_check = subprocess.run(
                    [
                        "nft",
                        "list",
                        "table",
                        str(lease["nft_family"]),
                        str(lease["nft_table"]),
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                require(nft_check.returncode != 0, f"nftables table remained: {lease['nft_table']}")

            print(
                "PREWARM_CONCURRENCY_SCENARIO "
                f"name=finite-pool round={round_number}/{rounds} users={users} "
                f"prewarm={len(prewarm_results)} cold={len(cold_results)} status=pass"
            )

        worker = start_worker(config_path, worker_log)
        duplicate_ready = wait_for_ready_exact(prewarm_store, ready_target)
        duplicate_ready_ids = {str(item["runtime_id"]) for item in duplicate_ready}
        stop_process(worker)
        worker = None

        duplicate_target = cases[0]
        operation_count_before = len(
            [
                row
                for row in query_rows("startup_operations")
                if row["logical_instance_id"] == duplicate_target["instanceId"]
            ]
        )
        lease_ids_before = {
            str(row["instance_id"]) for row in query_rows("network_leases")
        }
        require(
            lease_ids_before == duplicate_ready_ids,
            "duplicate-start baseline network leases do not match ready pool",
        )
        duplicate_cases = []
        for _ in range(2):
            duplicate_cases.append(
                {
                    **duplicate_target,
                    "opener": authenticated_opener(
                        duplicate_target["username"], duplicate_target["password"]
                    ),
                }
            )
        duplicate_results = concurrent_requests(
            duplicate_cases,
            "POST",
            "/start",
            capture_http_errors=True,
        )
        all_results.extend(duplicate_results)
        duplicate_successes = [
            result for result in duplicate_results if result["httpStatus"] == 200
        ]
        duplicate_rejections = [
            result for result in duplicate_results if result["httpStatus"] != 200
        ]
        require(len(duplicate_successes) == 1, "duplicate START did not produce one success")
        require(len(duplicate_rejections) == 1, "duplicate START did not produce one rejection")
        rejected = duplicate_rejections[0]
        require(rejected["httpStatus"] == 409, "duplicate START rejection was not HTTP 409")
        require(
            rejected["errorCode"] in {"instance_busy", "instance_running"},
            f"unexpected duplicate START error: {rejected['errorCode']}",
        )
        successful_response = duplicate_successes[0]["response"] or {}
        successful_runtime = successful_response.get("runtime") or {}
        successful_timing = successful_runtime.get("startupOperationTiming") or {}
        successful_runtime_id = str(successful_runtime.get("id"))
        require(
            successful_timing.get("strategy") == "prewarm",
            "duplicate START success did not claim a ready prewarmer",
        )
        require(
            successful_runtime_id in duplicate_ready_ids,
            "duplicate START claimed an unexpected runtime",
        )

        duplicate_bindings = [
            row
            for row in query_rows("runtime_bindings")
            if row["logical_instance_id"] == duplicate_target["instanceId"]
        ]
        duplicate_attachments = [
            row
            for row in query_rows("volume_attachments")
            if row["logical_instance_id"] == duplicate_target["instanceId"]
        ]
        duplicate_operations = [
            row
            for row in query_rows("startup_operations")
            if row["logical_instance_id"] == duplicate_target["instanceId"]
        ]
        duplicate_instances = [
            row
            for row in query_rows("instances")
            if row["id"] == duplicate_target["instanceId"]
        ]
        require(len(duplicate_bindings) == 1, "duplicate START created extra bindings")
        require(len(duplicate_attachments) == 1, "duplicate START created extra attachments")
        require(
            len(duplicate_operations) == operation_count_before + 1,
            "duplicate START created extra startup operations",
        )
        require(
            len(duplicate_instances) == 1 and duplicate_instances[0]["status"] == "running",
            "duplicate START did not leave the logical instance running",
        )
        require(
            {str(row["instance_id"]) for row in query_rows("network_leases")}
            == lease_ids_before,
            "duplicate START created an extra network lease",
        )

        stopped_duplicate = request_json(
            duplicate_target["opener"],
            "POST",
            f"/api/instances/{duplicate_target['instanceId']}/stop",
        )
        require(
            stopped_duplicate.get("status") == "stopped",
            "duplicate-start instance did not stop",
        )
        require(not query_rows("runtime_bindings"), "binding remained after duplicate-start stop")
        require(
            not query_rows("volume_attachments"),
            "attachment remained after duplicate-start stop",
        )

        for item in prewarm_store.list_ready(PROFILE_KEY):
            runtime_id = str(item["runtime_id"])
            require(prewarm_store.mark_draining(runtime_id), f"failed to drain {runtime_id}")
            cleanup_runtime(runtime, runtime_id)
            require(prewarm_store.retire(runtime_id), f"failed to retire {runtime_id}")
        require(
            not query_rows("network_leases"),
            "network lease remained after duplicate-start cleanup",
        )
        for runtime_id in duplicate_ready_ids:
            require(not (JAILER_ROOT / runtime_id).exists(), f"jail remained: {runtime_id}")
            require(
                not (PLACEHOLDER_DIR / f"{runtime_id}.placeholder.ext4").exists(),
                f"placeholder remained: {runtime_id}",
            )
        print(
            "PREWARM_CONCURRENCY_SCENARIO "
            "name=duplicate-start success=1 rejected=1 status=pass"
        )

        worker = start_worker(config_path, worker_log)
        for round_number in range(1, rounds + 1):
            wait_for_ready_exact(prewarm_store, ready_target)
            live_started = concurrent_requests(cases, "POST", "/start")
            all_results.extend(live_started)
            require(
                all(result["response"].get("status") == "running" for result in live_started),
                "one or more live-worker starts failed",
            )
            live_runtime_ids = []
            live_strategies = []
            for result in live_started:
                runtime_payload = result["response"].get("runtime") or {}
                timing = runtime_payload.get("startupOperationTiming") or {}
                runtime_id = runtime_payload.get("id")
                require(
                    isinstance(runtime_id, str) and runtime_id,
                    "live-worker start omitted runtime ID",
                )
                require(
                    timing.get("strategy") in {"prewarm", "cold"},
                    "live-worker start returned an unexpected strategy",
                )
                require(result["latencyMs"] <= 90_000, "live-worker start exceeded 90 seconds")
                live_runtime_ids.append(runtime_id)
                live_strategies.append(str(timing["strategy"]))
            require(
                len(set(live_runtime_ids)) == users,
                "live-worker starts reused a physical runtime",
            )

            runtime_rows = {
                str(row["instance_id"]): row for row in query_rows("runtime_instances")
            }
            live_runtime_rows = [
                runtime_rows.get(runtime_id) for runtime_id in live_runtime_ids
            ]
            require(
                all(row is not None for row in live_runtime_rows),
                "live Runtime metadata is missing",
            )
            require(
                len({int(row["pid"]) for row in live_runtime_rows if row is not None}) == users,
                "live-worker starts reused a Firecracker PID",
            )
            require(
                len({str(row["volume_path"]) for row in live_runtime_rows if row is not None})
                == users,
                "live-worker starts reused a data volume",
            )
            lease_runtime_ids = {
                str(row["instance_id"]) for row in query_rows("network_leases")
            }
            require(
                set(live_runtime_ids).issubset(lease_runtime_ids),
                "a live user runtime has no network lease",
            )
            prewarm_hits = live_strategies.count("prewarm")
            require(
                len(query_rows("runtime_bindings")) == prewarm_hits,
                "live-worker binding count does not match prewarm hits",
            )
            attachments = query_rows("volume_attachments")
            require(
                len(attachments) == prewarm_hits,
                "live-worker attachment count does not match prewarm hits",
            )
            require(
                len({str(row["volume_path"]) for row in attachments}) == len(attachments),
                "live-worker created duplicate volume attachments",
            )
            require(worker.poll() is None, "prewarm Worker exited during concurrent starts")

            live_stopped = concurrent_requests(cases, "POST", "/stop")
            all_results.extend(live_stopped)
            require(
                all(result["response"].get("status") == "stopped" for result in live_stopped),
                "one or more live-worker stops failed",
            )
            replenished = wait_for_ready_exact(prewarm_store, ready_target)
            replenished_ids = {str(item["runtime_id"]) for item in replenished}
            require(worker.poll() is None, "prewarm Worker exited before replenishment")
            require(not query_rows("runtime_bindings"), "binding remained after live-worker stop")
            require(
                not query_rows("volume_attachments"),
                "attachment remained after live-worker stop",
            )
            active_items = [
                item
                for item in query_rows("prewarm_items")
                if item["status"] in {"claiming", "assigned"}
            ]
            require(not active_items, "active prewarm item remained after live-worker stop")
            stopped_instances = [
                row
                for row in query_rows("instances")
                if row["id"] in {case["instanceId"] for case in cases}
            ]
            require(
                len(stopped_instances) == users
                and all(row["status"] == "stopped" for row in stopped_instances),
                "logical instance state did not settle at stopped",
            )
            require(
                {str(row["instance_id"]) for row in query_rows("network_leases")}
                == replenished_ids,
                "network leases do not match the replenished pool",
            )
            running_runtime_ids = {
                str(row["instance_id"])
                for row in query_rows("runtime_instances")
                if row["status"] == "running"
            }
            require(
                running_runtime_ids == replenished_ids,
                "running Runtime metadata does not match the replenished pool",
            )
            for runtime_id in live_runtime_ids:
                require(
                    not (JAILER_ROOT / runtime_id).exists(),
                    f"retired jail remained: {runtime_id}",
                )
                require(
                    not (PLACEHOLDER_DIR / f"{runtime_id}.placeholder.ext4").exists(),
                    f"retired placeholder remained: {runtime_id}",
                )
            print(
                "PREWARM_CONCURRENCY_SCENARIO "
                f"name=live-worker round={round_number}/{rounds} users={users} "
                f"prewarm={prewarm_hits} cold={live_strategies.count('cold')} "
                "pool=replenished status=pass"
            )

        stop_process(worker)
        worker = None
        for item in prewarm_store.list_ready(PROFILE_KEY):
            runtime_id = str(item["runtime_id"])
            require(prewarm_store.mark_draining(runtime_id), f"failed to drain {runtime_id}")
            cleanup_runtime(runtime, runtime_id)
            require(prewarm_store.retire(runtime_id), f"failed to retire {runtime_id}")
        require(
            not query_rows("network_leases"),
            "network lease remained after live-worker cleanup",
        )

        for case in cases:
            request_json(case["opener"], "DELETE", f"/api/instances/{case['instanceId']}")

        print("PASS: real prewarm concurrency invariants remained valid")
        print(
            "PREWARM_CONCURRENCY_READY "
            f"users={users} ready={ready_target} rounds={rounds} "
            "unique=verified duplicate_start=verified live_worker=verified "
            "volume=exclusive network=clean pool=replenished"
        )
    except Exception as error:
        stop_process(worker)
        worker = None
        stop_process(server)
        server = None
        archive = archive_failure(error, all_results, server_log, worker_log)
        print(f"PREWARM_CONCURRENCY_FAILURE_ARCHIVED path={archive}", file=sys.stderr)
        raise
    finally:
        stop_process(worker)
        stop_process(server)
        runtime_ids: set[str] = set()
        if DATABASE.is_file():
            try:
                runtime_ids.update(
                    str(row["instance_id"]) for row in query_rows("runtime_instances")
                )
            except sqlite3.Error:
                pass
        for runtime_id in sorted(runtime_ids):
            cleanup_runtime(runtime, runtime_id)
        for case in cases:
            Path(case["volumePath"]).unlink(missing_ok=True)
        WORKER_LOCK.unlink(missing_ok=True)
        shutil.rmtree(STATE_ROOT, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FAIL: prewarm concurrency test: {error}", file=sys.stderr)
        raise SystemExit(1)
