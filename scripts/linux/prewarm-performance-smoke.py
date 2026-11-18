#!/usr/bin/env python3
"""阶段 6.2：冷启动 vs 预热命中的可重复 A/B 性能采样。

通过真实 Control Plane 交替采集冷启动与预热命中样本，每条样本记录
``clientObservedMs``、``startupOperationTiming.totalMs``、strategy 和分阶段计时。
池通过改写 ``PREWARM_TARGET_READY`` 让 Worker 热重载来切换「有空闲 ready 项」
与「池空回退冷启动」，保证两组使用同一环境、同一用户数据盘。
"""

from __future__ import annotations

import http.cookiejar
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
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
from prewarm_config import PrewarmConfig  # noqa: E402
from prewarm_store import PrewarmStore  # noqa: E402
from runtime import InstanceRuntime  # noqa: E402
from runtime_store import RuntimeStore, VolumeAttachmentStore  # noqa: E402


STATE_ROOT = Path("/var/lib/fc/prewarm-performance")
DATABASE = STATE_ROOT / "control-plane.db"
VOLUME_ROOT = Path("/srv/fc/volumes/users")
JAILER_ROOT = Path("/srv/jailer/firecracker")
PLACEHOLDER_DIR = VOLUME_ROOT.parent / "prewarm"
PORT = 18098
BASE_URL = f"http://127.0.0.1:{PORT}"
PROFILE_KEY = "default-2vcpu-1g"
WORKER_LOCK = Path("/run/ai-agent-prewarm-performance.lock")
RESULT_ROOT = Path("/var/lib/fc/prewarm-performance-results")
DEFAULT_SAMPLES = 30
WARMUP_SAMPLES = 5
GROUP_SIZE = 5


DIAGNOSTIC_TABLES = (
    "instances",
    "runtime_bindings",
    "runtime_instances",
    "runtime_sidecars",
    "prewarm_items",
    "volume_attachments",
    "startup_operations",
    "network_leases",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def database_snapshot(database: Path) -> dict:
    """在同一个 SQLite 读事务中抓取竞态相关表，避免拼接不同时间点的状态。"""
    if not database.is_file():
        return {"database": str(database), "missing": True, "tables": {}}
    connection = sqlite3.connect(database, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        tables: dict[str, list[dict] | dict] = {}
        for table in DIAGNOSTIC_TABLES:
            if table not in existing:
                tables[table] = {"missing": True}
                continue
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()
            tables[table] = [dict(row) for row in rows]
        return {
            "database": str(database),
            "capturedAt": int(time.time()),
            "tables": tables,
        }
    finally:
        connection.rollback()
        connection.close()


def copy_database(database: Path, destination: Path) -> None:
    """使用 SQLite backup API 复制一致数据库，而不是直接复制 WAL 文件组合。"""
    if not database.is_file():
        return
    source = sqlite3.connect(database, timeout=10)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def command_output(arguments: list[str]) -> str:
    try:
        completed = subprocess.run(arguments, check=False, capture_output=True, text=True)
    except OSError as error:
        return f"ERROR running {arguments!r}: {error}\n"
    return (completed.stdout or "") + (completed.stderr or "")


def archive_failure(
    error: Exception,
    samples: list[dict],
    logical_id: str,
    server_log: Path,
    worker_log: Path,
) -> Path:
    """在清理测试资源之前固化失败现场，供 STOP 竞态离线复盘。"""
    result_dir = RESULT_ROOT / (
        "failed-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    )
    result_dir.mkdir(parents=True, mode=0o755, exist_ok=False)
    snapshot = database_snapshot(DATABASE)
    (result_dir / "state-snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (result_dir / "samples.partial.jsonl").write_text(
        "".join(json.dumps(sample, ensure_ascii=False) + "\n" for sample in samples),
        encoding="utf-8",
    )
    (result_dir / "failure.json").write_text(
        json.dumps(
            {
                "capturedAt": int(time.time()),
                "logicalInstanceId": logical_id,
                "errorType": type(error).__name__,
                "error": str(error),
                "traceback": traceback.format_exc(),
                "completedSamples": len(samples),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    copy_database(DATABASE, result_dir / "control-plane.db")
    for name, source in (
        ("control-plane.log", server_log),
        ("worker.log", worker_log),
    ):
        if source.exists():
            shutil.copy2(source, result_dir / name)
    (result_dir / "process.txt").write_text(
        command_output(["ps", "-eo", "pid,ppid,user,stat,lstart,args"]),
        encoding="utf-8",
    )
    (result_dir / "network.txt").write_text(
        command_output(["ip", "-details", "link", "show"])
        + "\n--- routes ---\n"
        + command_output(["ip", "-4", "route", "show"])
        + "\n--- nftables ---\n"
        + command_output(["nft", "list", "tables"]),
        encoding="utf-8",
    )
    pressure = []
    for source in (
        Path("/proc/loadavg"),
        Path("/proc/meminfo"),
        Path("/proc/pressure/cpu"),
        Path("/proc/pressure/io"),
        Path("/proc/pressure/memory"),
    ):
        try:
            pressure.append(f"--- {source} ---\n{source.read_text(encoding='utf-8')}")
        except OSError as read_error:
            pressure.append(f"--- {source} ---\nERROR: {read_error}\n")
    (result_dir / "pressure.txt").write_text("\n".join(pressure), encoding="utf-8")
    return result_dir


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


def write_config(config_path: Path, target_ready: int) -> None:
    config_path.write_text(
        "PREWARM_ENABLED=1\n"
        f"PREWARM_TARGET_READY={target_ready}\n"
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


def wait_for_ready(store: PrewarmStore, timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = store.list_ready(PROFILE_KEY)
        if items:
            return items[0]
        time.sleep(0.5)
    raise RuntimeError("no ready prewarm item within timeout")


def wait_for_pool_empty(store: PrewarmStore, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not store.list_ready(PROFILE_KEY):
            return
        time.sleep(0.5)
    raise RuntimeError("prewarm pool did not drain within timeout")


def percentile(sorted_values: list[int], ratio: float) -> int | None:
    if not sorted_values:
        return None
    index = math.ceil(ratio * len(sorted_values)) - 1
    index = max(0, min(index, len(sorted_values) - 1))
    return sorted_values[index]


def summarize_values(raw_values: list[int | float]) -> dict:
    values = sorted(int(value) for value in raw_values)
    if not values:
        return {}
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return {
        "count": len(values),
        "min": values[0],
        "max": values[-1],
        "mean": round(mean, 2),
        "p50": percentile(values, 0.5),
        "p95": percentile(values, 0.95),
        "stddev": round(math.sqrt(variance), 2),
    }


def summarize(name: str, samples: list[dict]) -> dict:
    return summarize_values(
        [sample[name] for sample in samples if sample.get("success") and sample.get(name) is not None]
    )


def summarize_milestone(name: str, samples: list[dict]) -> dict:
    return summarize_values(
        [
            sample["milestones"][name]
            for sample in samples
            if sample.get("success")
            and isinstance(sample.get("milestones"), dict)
            and sample["milestones"].get(name) is not None
        ]
    )


def read_mem_available_kib() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def read_process_memory_kib(pid: int) -> dict[str, int | None]:
    result: dict[str, int | None] = {"rssKib": None, "pssKib": None}
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text(
            encoding="utf-8"
        ).splitlines():
            if line.startswith("Rss:"):
                result["rssKib"] = int(line.split()[1])
            elif line.startswith("Pss:"):
                result["pssKib"] = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return result


def active_network_lease_count(database: Path) -> int | None:
    try:
        with sqlite3.connect(database, timeout=10) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM network_leases WHERE status IN ('reserved','active')"
            ).fetchone()
        return int(row[0]) if row is not None else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


def capture_pool_resource(
    state: str,
    *,
    ready_item: dict | None = None,
    prepare_wait_ms: int | None = None,
) -> dict:
    """记录池空/预热 ready 时的 Host 资源快照。"""
    snapshot: dict = {
        "state": state,
        "capturedAt": int(time.time()),
        "memAvailableKib": read_mem_available_kib(),
        "activeNetworkLeases": active_network_lease_count(DATABASE),
    }
    if ready_item is not None:
        snapshot["runtimeId"] = str(ready_item.get("runtime_id") or "")
        pid = int(ready_item.get("vmm_pid") or 0)
        snapshot["vmmPid"] = pid or None
        snapshot["prepareWaitMs"] = prepare_wait_ms
        if pid:
            snapshot.update(read_process_memory_kib(pid))
    return snapshot


def evaluate_summary(summary: dict, expected_samples: int) -> dict:
    cold_p95 = int((summary.get("cold") or {}).get("p95") or 0)
    prewarm_p95 = int((summary.get("prewarm") or {}).get("p95") or 0)
    reduction = (
        round((cold_p95 - prewarm_p95) * 100 / cold_p95, 2)
        if cold_p95 > 0
        else 0.0
    )
    checks = {
        "coldSampleCount": (summary.get("cold") or {}).get("count") == expected_samples,
        "prewarmSampleCount": (summary.get("prewarm") or {}).get("count")
        == expected_samples,
        "coldSuccessRate": summary.get("coldSuccessRate") == 1.0,
        "prewarmSuccessRate": summary.get("prewarmSuccessRate") == 1.0,
        "strategyFallbackRate": summary.get("strategyFallbackRate") == 0.0,
        "platformP95ReductionAtLeast40Percent": reduction >= 40.0,
        "guestMilestonesPresent": int(
            (summary.get("coldGuestDeclaredRpcReadyMs") or {}).get("p95") or 0
        )
        > 0
        and int(
            (summary.get("prewarmGuestDeclaredRpcReadyMs") or {}).get("p95") or 0
        )
        > 0,
    }
    return {
        "passed": all(checks.values()),
        "platformP95ReductionPercent": reduction,
        "requiredReductionPercent": 40.0,
        "checks": checks,
    }


def capture_environment(runtime: InstanceRuntime) -> dict:
    def run(cmd: list[str]) -> str:
        completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
        return (completed.stdout or completed.stderr).strip()[-2000:]

    env: dict = {"capturedAt": int(time.time())}
    # 提交哈希由宿主机 run.sh 注入（VM 上 rsync 不携带 .git）。
    env["gitCommit"] = os.environ.get("GIT_COMMIT", "")
    try:
        env["kernel"] = run(["uname", "-r"])
        env["firecracker"] = run(["firecracker", "--version"])
    except Exception:
        pass
    try:
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
        mem_available = next(
            (line for line in meminfo.splitlines() if line.startswith("MemAvailable")),
            "",
        )
        env["memAvailable"] = mem_available.split(":")[-1].strip()
    except Exception:
        pass
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        env["cpuModel"] = next(
            (line.split(":")[-1].strip() for line in cpuinfo.splitlines()
             if line.startswith("model name") or line.startswith("Model")),
            "",
        )
        env["cpuCount"] = sum(
            1 for line in cpuinfo.splitlines() if line.startswith("processor")
        )
    except Exception:
        pass
    return env


def run_cycle(
    opener,
    store: PrewarmStore,
    logical_id: str,
    runtime: InstanceRuntime,
    attachment_store: VolumeAttachmentStore,
    runtime_store: RuntimeStore,
    volume_path: Path,
) -> dict:
    """执行一次启动→校验→停止，返回样本；strategy 由调用方预先控制池状态决定。"""
    started_at = time.monotonic()
    started = request_json(opener, "POST", f"/api/instances/{logical_id}/start")
    client_observed_ms = round((time.monotonic() - started_at) * 1000)

    runtime_payload = started.get("runtime") or {}
    timing = runtime_payload.get("startupOperationTiming") or {}
    strategy = timing.get("strategy") or started.get("strategy") or "unknown"
    require(strategy in {"cold", "prewarm"}, f"unexpected strategy: {strategy}")
    runtime_id = runtime_payload.get("id", "")

    try:
        stopped = request_json(opener, "POST", f"/api/instances/{logical_id}/stop")
    except RuntimeError as error:
        # PREWARM_RACE_DIAGNOSTIC：不要再调用会触发 reconcile 的 GET；直接在
        # 同一 SQLite 读事务中保留各层状态，避免诊断本身继续修改现场。
        try:
            snapshot = database_snapshot(DATABASE)
            tables = snapshot.get("tables") or {}
            detail = " database_snapshot=" + json.dumps(
                {
                    "instances": tables.get("instances"),
                    "runtime_bindings": tables.get("runtime_bindings"),
                    "runtime_instances": tables.get("runtime_instances"),
                    "prewarm_items": tables.get("prewarm_items"),
                    "volume_attachments": tables.get("volume_attachments"),
                    "startup_operations": tables.get("startup_operations"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except Exception as diagnostic_error:
            detail = f" diagnostic_error={diagnostic_error}"
        if not detail:
            detail = ""
        raise RuntimeError(f"{error}; strategy={strategy} runtime_id={runtime_id}{detail}") from error
    require(stopped.get("status") == "stopped", "Control Plane did not stop instance")

    require(runtime_store.get_binding(logical_id) is None, "binding remained after stop")
    require(attachment_store.get(str(volume_path)) is None, "attachment remained after stop")
    # 预热走 destroy，物理资源必须全清；冷启动只 stop，不做这些断言。
    if strategy == "prewarm" and runtime_id:
        require(runtime_store.get(runtime_id) is None, "physical runtime metadata remained")
        require(runtime.network_manager.store.get(runtime_id) is None, "network lease remained")
        require(not (JAILER_ROOT / runtime_id).exists(), "jail remained")
        require(not (PLACEHOLDER_DIR / f"{runtime_id}.placeholder.ext4").exists(), "placeholder remained")

    return {
        "sampleId": uuid.uuid4().hex[:12],
        "strategy": strategy,
        "success": True,
        "clientObservedMs": client_observed_ms,
        "totalMs": timing.get("totalMs"),
        "phases": timing.get("phases"),
        "milestones": timing.get("milestones"),
        "runtimeId": runtime_id,
        "timestamp": int(time.time()),
    }


def sample_group(
    opener,
    store: PrewarmStore,
    logical_id: str,
    runtime: InstanceRuntime,
    attachment_store: VolumeAttachmentStore,
    runtime_store: RuntimeStore,
    volume_path: Path,
    config_path: Path,
    strategy: str,
    count: int,
    samples: list[dict],
    resource_samples: list[dict],
    label: str,
) -> None:
    """采集 count 个指定 strategy 样本；交替改写 target_ready 控制池。"""
    target_ready = 1 if strategy == "prewarm" else 0
    write_config(config_path, target_ready)
    if strategy == "prewarm":
        ready_wait_started = time.monotonic()
        ready_item = wait_for_ready(store)
        ready_wait_ms = round((time.monotonic() - ready_wait_started) * 1000)
    else:
        wait_for_pool_empty(store)
        ready_item = None
        ready_wait_ms = None
        resource_samples.append(capture_pool_resource("pool-empty"))
    for _ in range(count):
        if ready_item is not None:
            resource_samples.append(
                capture_pool_resource(
                    "ready",
                    ready_item=ready_item,
                    prepare_wait_ms=ready_wait_ms,
                )
            )
        sample = run_cycle(
            opener, store, logical_id, runtime, attachment_store, runtime_store, volume_path
        )
        require(sample["strategy"] == strategy, f"sample strategy {sample['strategy']} != {strategy}")
        sample["requestedStrategy"] = strategy
        samples.append(sample)
        strategy_count = sum(1 for item in samples if item["strategy"] == strategy)
        if label.isdigit():
            progress = (
                f"strategyCount={strategy_count}/{label} "
                f"totalCount={len(samples)}/{int(label) * 2}"
            )
        else:
            progress = f"strategyCount={strategy_count}/{label} totalCount={len(samples)}"
        print(
            f"PREWARM_PERF_SAMPLE strategy={strategy} clientObservedMs={sample['clientObservedMs']} "
            f"totalMs={sample['totalMs']} {progress}"
        )
        if strategy == "prewarm":
            ready_wait_started = time.monotonic()
            ready_item = wait_for_ready(store)
            ready_wait_ms = round((time.monotonic() - ready_wait_started) * 1000)
        else:
            wait_for_pool_empty(store)


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
    print(
        "PREWARM_DESTRUCTIVE_ACTION actor=performance-test-cleanup "
        f"action=destroy-runtime runtime={runtime_id}",
        file=sys.stderr,
    )
    try:
        status = runtime.status(runtime_id)
        if status is not None and status.get("processAlive") is True:
            runtime.stop(runtime_id, reason="prewarm_perf_cleanup")
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
    samples_per_strategy = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SAMPLES
    if not 1 <= samples_per_strategy <= 200:
        raise RuntimeError("sample count must be between 1 and 200")
    if STATE_ROOT.exists():
        raise RuntimeError(f"test state already exists; inspect it first: {STATE_ROOT}")

    suffix = uuid.uuid4().hex[:10]
    logical_id = f"perf-user-{suffix}"
    username = f"perf-{suffix}"
    password = f"Perf-{suffix}-{uuid.uuid4().hex[:12]}"
    credential = STATE_ROOT / "placeholder-api-key"
    config_path = STATE_ROOT / "prewarm.env"
    server_log = STATE_ROOT / "control-plane.log"
    worker_log = STATE_ROOT / "worker.log"
    server: subprocess.Popen | None = None
    worker: subprocess.Popen | None = None
    user_id = ""
    volume_path: Path | None = None
    current_runtime_id = ""

    STATE_ROOT.mkdir(parents=True, mode=0o755)
    credential.write_text("prewarm-performance-placeholder\n", encoding="utf-8")
    credential.chmod(0o600)
    write_config(config_path, 1)

    runtime = InstanceRuntime(
        state_root=STATE_ROOT,
        volume_root=VOLUME_ROOT,
        state_database=DATABASE,
        snapshot_root=STATE_ROOT / "snapshots",
        deepseek_credential=credential,
    )
    store = PrewarmStore(DATABASE)
    attachment_store = VolumeAttachmentStore(DATABASE)
    runtime_store = RuntimeStore(DATABASE)

    samples: list[dict] = []
    resource_samples: list[dict] = []
    try:
        user = AuthStore(DATABASE).create_user(username, password, claim_unowned=False)
        user_id = str(user["id"])
        volume_path = runtime.user_volume_path(user_id)
        uid, gid, _, _ = runtime.ensure_host()
        runtime.create_volume(volume_path, uid, gid)  # 预创建，避免首次建盘混入样本

        log_handle = server_log.open("ab", buffering=0)
        server = subprocess.Popen(
            [
                sys.executable, str(CONTROL_PLANE / "server.py"),
                "--listen-host", "127.0.0.1",
                "--listen-port", str(PORT),
                "--database", str(DATABASE),
                "--state-root", str(STATE_ROOT),
                "--deepseek-credential", str(credential),
                "--prewarm-config", str(config_path),
                "--require-auth",
            ],
            stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_handle.close()
        wait_for_control_plane(server, server_log)

        worker_log_handle = worker_log.open("ab", buffering=0)
        worker = subprocess.Popen(
            [
                sys.executable, str(INSTANCE_MANAGER / "prewarm_worker_main.py"),
                "--database", str(DATABASE),
                "--state-root", str(STATE_ROOT),
                "--prewarm-config", str(config_path),
                "--profile-key", PROFILE_KEY,
                "--lock-file", str(WORKER_LOCK),
                "--max-backoff", "5",
            ],
            stdin=subprocess.DEVNULL, stdout=worker_log_handle, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        worker_log_handle.close()

        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        request_json(opener, "POST", "/api/auth/login", {"username": username, "password": password})
        created = request_json(opener, "POST", "/api/instances", {"id": logical_id})
        require(created.get("status") == "created", "logical instance was not created")

        # 热身：冷/预热各若干轮，不计入样本。
        for strategy in ("prewarm", "cold"):
            sample_group(
                opener, store, logical_id, runtime, attachment_store, runtime_store,
                volume_path, config_path, strategy, WARMUP_SAMPLES, samples,
                resource_samples, "warmup",
            )
        samples.clear()
        resource_samples.clear()

        # 正式采样：按组交错，避免时间漂移偏向某一组。
        remaining = samples_per_strategy
        while remaining > 0:
            group = min(GROUP_SIZE, remaining)
            sample_group(
                opener, store, logical_id, runtime, attachment_store, runtime_store,
                volume_path, config_path, "cold", group, samples, resource_samples,
                f"{samples_per_strategy}",
            )
            sample_group(
                opener, store, logical_id, runtime, attachment_store, runtime_store,
                volume_path, config_path, "prewarm", group, samples, resource_samples,
                f"{samples_per_strategy}",
            )
            remaining -= group

        request_json(opener, "DELETE", f"/api/instances/{logical_id}")

        cold_samples = [s for s in samples if s["strategy"] == "cold"]
        prewarm_samples = [s for s in samples if s["strategy"] == "prewarm"]
        require(len(cold_samples) >= samples_per_strategy, "not enough cold samples")
        require(len(prewarm_samples) >= samples_per_strategy, "not enough prewarm samples")

        ready_resources = [item for item in resource_samples if item["state"] == "ready"]
        empty_resources = [
            item for item in resource_samples if item["state"] == "pool-empty"
        ]
        ready_mem = summarize_values(
            [item["memAvailableKib"] for item in ready_resources if item.get("memAvailableKib") is not None]
        )
        empty_mem = summarize_values(
            [item["memAvailableKib"] for item in empty_resources if item.get("memAvailableKib") is not None]
        )
        idle_resource_cost = {
            "sampleCount": len(ready_resources),
            "vmmRssKib": summarize_values(
                [item["rssKib"] for item in ready_resources if item.get("rssKib") is not None]
            ),
            "vmmPssKib": summarize_values(
                [item["pssKib"] for item in ready_resources if item.get("pssKib") is not None]
            ),
            "replenishWaitMs": summarize_values(
                [
                    item["prepareWaitMs"]
                    for item in ready_resources
                    if item.get("prepareWaitMs") is not None
                ]
            ),
            "poolEmptyMemAvailableKib": empty_mem,
            "readyMemAvailableKib": ready_mem,
            "estimatedHostMemoryCostKib": (
                max(0, round(empty_mem["mean"] - ready_mem["mean"]))
                if empty_mem and ready_mem
                else None
            ),
            "activeNetworkLeases": summarize_values(
                [
                    item["activeNetworkLeases"]
                    for item in ready_resources
                    if item.get("activeNetworkLeases") is not None
                ]
            ),
        }
        summary = {
            "samplesPerStrategy": samples_per_strategy,
            "cold": summarize("clientObservedMs", cold_samples),
            "prewarm": summarize("clientObservedMs", prewarm_samples),
            "coldTotalMs": summarize("totalMs", cold_samples),
            "prewarmTotalMs": summarize("totalMs", prewarm_samples),
            "coldGuestDeclaredRpcReadyMs": summarize_milestone(
                "guestDeclaredRpcReadyMs", cold_samples
            ),
            "prewarmGuestDeclaredRpcReadyMs": summarize_milestone(
                "guestDeclaredRpcReadyMs", prewarm_samples
            ),
            "coldSuccessRate": round(
                sum(1 for sample in cold_samples if sample.get("success"))
                / samples_per_strategy,
                4,
            ),
            "prewarmSuccessRate": round(
                sum(1 for sample in prewarm_samples if sample.get("success"))
                / samples_per_strategy,
                4,
            ),
            "strategyFallbackRate": round(
                sum(
                    1
                    for sample in samples
                    if sample.get("requestedStrategy") != sample.get("strategy")
                )
                / len(samples),
                4,
            ),
            "idlePrewarmResourceCost": idle_resource_cost,
        }
        summary["verdict"] = evaluate_summary(summary, samples_per_strategy)
        environment = capture_environment(runtime)

        result_dir = RESULT_ROOT / time.strftime("%Y%m%d-%H%M%S")
        result_dir.mkdir(parents=True, mode=0o755, exist_ok=True)
        (result_dir / "samples.jsonl").write_text(
            "".join(json.dumps(sample, ensure_ascii=False) + "\n" for sample in samples),
            encoding="utf-8",
        )
        (result_dir / "resource-samples.jsonl").write_text(
            "".join(json.dumps(sample, ensure_ascii=False) + "\n" for sample in resource_samples),
            encoding="utf-8",
        )
        (result_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (result_dir / "environment.json").write_text(
            json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for name in ("control-plane.log", "worker.log"):
            source = STATE_ROOT / name
            if source.exists():
                shutil.copy2(source, result_dir / name)

        print(
            "PREWARM_PERF_READY "
            f"samples={len(samples)} coldP95={summary['cold'].get('p95')} "
            f"prewarmP95={summary['prewarm'].get('p95')} "
            f"reductionPercent={summary['verdict']['platformP95ReductionPercent']} "
            f"verdict={'pass' if summary['verdict']['passed'] else 'fail'} "
            f"output={result_dir}"
        )
        require(
            summary["verdict"]["passed"],
            f"stage 6.3 performance verdict failed; report={result_dir / 'summary.json'}",
        )
    except Exception as error:
        # 先停止两个会继续改写数据库的服务，再固化 SQLite/日志/进程/网络现场。
        stop_process(worker)
        worker = None
        stop_process(server)
        server = None
        try:
            failure_archive = archive_failure(
                error,
                samples,
                logical_id,
                server_log,
                worker_log,
            )
            print(
                f"PREWARM_PERF_FAILURE_ARCHIVED path={failure_archive}",
                file=sys.stderr,
            )
        except Exception as archive_error:
            print(
                f"PREWARM_PERF_ARCHIVE_ERROR error={archive_error}",
                file=sys.stderr,
            )
        for path in (server_log, worker_log):
            if path.exists():
                print(f"--- {path.name} ---", file=sys.stderr)
                print(path.read_text(encoding="utf-8", errors="replace")[-12000:], file=sys.stderr)
        raise
    finally:
        stop_process(worker)
        stop_process(server)
        # 即使失败现场已归档，也要清理所有登记 Runtime，避免诊断测试留下 VMM、
        # TAP 或网络租约。先从数据库获取 ID，不能只清 ready 项。
        runtime_ids: set[str] = set()
        try:
            snapshot = database_snapshot(DATABASE)
            runtime_rows = (snapshot.get("tables") or {}).get("runtime_instances") or []
            if isinstance(runtime_rows, list):
                runtime_ids.update(
                    str(row["instance_id"])
                    for row in runtime_rows
                    if isinstance(row, dict) and row.get("instance_id")
                )
        except Exception as diagnostic_error:
            print(f"CLEANUP_WARNING database snapshot failed: {diagnostic_error}", file=sys.stderr)
        for ready in store.list_ready(PROFILE_KEY):
            runtime_ids.add(str(ready["runtime_id"]))
        if current_runtime_id:
            runtime_ids.add(current_runtime_id)
        for runtime_id in sorted(runtime_ids):
            cleanup_runtime(runtime, runtime_id)
        if volume_path is not None:
            volume_path.unlink(missing_ok=True)
        WORKER_LOCK.unlink(missing_ok=True)
        shutil.rmtree(STATE_ROOT, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FAIL: prewarm performance test: {error}", file=sys.stderr)
        raise SystemExit(1)
