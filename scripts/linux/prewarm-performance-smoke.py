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


def summarize(name: str, samples: list[dict]) -> dict:
    values = sorted(int(sample[name]) for sample in samples if sample.get("success"))
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
        # 诊断：查询实例真实状态，区分「reconcile 误判 stopped」还是「真的非 running」。
        try:
            current = request_json(opener, "GET", f"/api/instances/{logical_id}")
            runtime_status = current.get("runtime") or {}
            detail = (
                f" instance_status={current.get('status')} "
                f"runtime_alive={runtime_status.get('processAlive')} "
                f"runtime_status={runtime_status.get('status')}"
            )
        except Exception:
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
    label: str,
) -> None:
    """采集 count 个指定 strategy 样本；交替改写 target_ready 控制池。"""
    target_ready = 1 if strategy == "prewarm" else 0
    write_config(config_path, target_ready)
    if strategy == "prewarm":
        wait_for_ready(store)
    else:
        wait_for_pool_empty(store)
    for _ in range(count):
        sample = run_cycle(
            opener, store, logical_id, runtime, attachment_store, runtime_store, volume_path
        )
        require(sample["strategy"] == strategy, f"sample strategy {sample['strategy']} != {strategy}")
        samples.append(sample)
        print(
            f"PREWARM_PERF_SAMPLE strategy={strategy} clientObservedMs={sample['clientObservedMs']} "
            f"totalMs={sample['totalMs']} count={len(samples)}/{label}"
        )
        if strategy == "prewarm":
            wait_for_ready(store)
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
                volume_path, config_path, strategy, WARMUP_SAMPLES, samples, "warmup",
            )
        samples.clear()

        # 正式采样：按组交错，避免时间漂移偏向某一组。
        remaining = samples_per_strategy
        while remaining > 0:
            group = min(GROUP_SIZE, remaining)
            sample_group(
                opener, store, logical_id, runtime, attachment_store, runtime_store,
                volume_path, config_path, "cold", group, samples, f"{samples_per_strategy}",
            )
            sample_group(
                opener, store, logical_id, runtime, attachment_store, runtime_store,
                volume_path, config_path, "prewarm", group, samples, f"{samples_per_strategy}",
            )
            remaining -= group

        request_json(opener, "DELETE", f"/api/instances/{logical_id}")

        cold_samples = [s for s in samples if s["strategy"] == "cold"]
        prewarm_samples = [s for s in samples if s["strategy"] == "prewarm"]
        require(len(cold_samples) >= samples_per_strategy, "not enough cold samples")
        require(len(prewarm_samples) >= samples_per_strategy, "not enough prewarm samples")

        summary = {
            "samplesPerStrategy": samples_per_strategy,
            "cold": summarize("clientObservedMs", cold_samples),
            "prewarm": summarize("clientObservedMs", prewarm_samples),
            "coldTotalMs": summarize("totalMs", cold_samples),
            "prewarmTotalMs": summarize("totalMs", prewarm_samples),
        }
        environment = capture_environment(runtime)

        result_dir = RESULT_ROOT / time.strftime("%Y%m%d-%H%M%S")
        result_dir.mkdir(parents=True, mode=0o755, exist_ok=True)
        (result_dir / "samples.jsonl").write_text(
            "".join(json.dumps(sample, ensure_ascii=False) + "\n" for sample in samples),
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
            f"output={result_dir}"
        )
    except Exception:
        for path in (server_log, worker_log):
            if path.exists():
                print(f"--- {path.name} ---", file=sys.stderr)
                print(path.read_text(encoding="utf-8", errors="replace")[-12000:], file=sys.stderr)
        raise
    finally:
        stop_process(worker)
        stop_process(server)
        for ready in store.list_ready(PROFILE_KEY):
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
        print(f"FAIL: prewarm performance test: {error}", file=sys.stderr)
        raise SystemExit(1)
