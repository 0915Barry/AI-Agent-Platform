#!/usr/bin/env python3
"""验证真实 Worker 进程锁冲突、无副作用退出和锁释放。"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_MAIN = REPO_ROOT / "services" / "instance-manager" / "prewarm_worker_main.py"
STATE_ROOT = Path("/var/lib/fc/prewarm-worker-lock-smoke")
DATABASE = STATE_ROOT / "control-plane.db"
CONFIG = STATE_ROOT / "prewarm.env"
LOG = STATE_ROOT / "worker.log"
LOCK = Path("/run/ai-agent-prewarm-worker-smoke.lock")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def command(*extra: str) -> list[str]:
    return [
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
        *extra,
    ]


def wait_ready(process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if LOG.exists() and "PREWARM_WORKER_READY" in LOG.read_text(
            encoding="utf-8", errors="replace"
        ):
            return
        time.sleep(0.1)
    tail = LOG.read_text(encoding="utf-8", errors="replace")[-4000:] if LOG.exists() else ""
    raise RuntimeError(f"first Worker did not become ready\n{tail}")


def table_count(table: str) -> int:
    with sqlite3.connect(DATABASE) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists is None:
            return 0
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def main() -> None:
    if STATE_ROOT.exists():
        raise RuntimeError(f"test state already exists; inspect it first: {STATE_ROOT}")
    STATE_ROOT.mkdir(parents=True, mode=0o755)
    LOCK.unlink(missing_ok=True)
    CONFIG.write_text(
        "PREWARM_ENABLED=0\n"
        "PREWARM_HEALTH_INTERVAL_SECONDS=1\n",
        encoding="utf-8",
    )

    first: subprocess.Popen | None = None
    try:
        with LOG.open("ab", buffering=0) as log_handle:
            first = subprocess.Popen(
                command(),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        wait_ready(first)
        require(LOCK.read_text(encoding="ascii").strip() == str(first.pid), "lock PID mismatch")

        second = subprocess.run(
            command("--once"),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
        require(second.returncode == 2, "second Worker did not report lock conflict")
        require(
            "PREWARM_WORKER_ALREADY_RUNNING" in second.stdout,
            "second Worker did not emit lock conflict marker",
        )
        require(table_count("prewarm_items") == 0, "lock loser created a prewarm item")
        require(table_count("network_leases") == 0, "lock loser allocated a network lease")

        first.terminate()
        first.wait(timeout=10)
        first = None

        third = subprocess.run(
            command("--once"),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
        require(third.returncode == 0, "Worker lock was not released after process exit")
        require(
            "PREWARM_WORKER_RECONCILE status=ok" in third.stdout,
            "replacement Worker did not complete reconcile",
        )
        require(table_count("prewarm_items") == 0, "disabled replacement created an item")
        require(table_count("network_leases") == 0, "disabled replacement allocated a lease")

        print("PASS: Worker process lock rejected a concurrent instance without side effects")
        print(
            "PREWARM_WORKER_LOCK_READY conflict=exit-2 runtime=absent "
            "lease=absent release=reacquired"
        )
    finally:
        if first is not None and first.poll() is None:
            first.terminate()
            try:
                first.wait(timeout=5)
            except subprocess.TimeoutExpired:
                first.kill()
                first.wait(timeout=2)
        LOCK.unlink(missing_ok=True)
        shutil.rmtree(STATE_ROOT, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FAIL: prewarm Worker lock smoke test: {error}", file=sys.stderr)
        raise SystemExit(1)
