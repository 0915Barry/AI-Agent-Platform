#!/usr/bin/env python3
"""预热 Worker 进程入口。"""

from __future__ import annotations

import argparse
import signal
import threading
from pathlib import Path
from typing import Callable

from prewarm_capacity import LinuxHostCapacityProbe
from prewarm_claim_audit import PrewarmClaimAuditor
from prewarm_claim_compensation import (
    ActivationTimeoutClaimCompensator,
    CommitPendingClaimCompensator,
    PreActivationClaimCompensator,
)
from prewarm_config import DEFAULT_CONFIG_PATH, PrewarmConfig
from prewarm_store import PrewarmStore
from prewarm_worker import PrewarmWorker
from prewarm_worker_lock import (
    DEFAULT_LOCK_PATH,
    PrewarmWorkerLock,
    WorkerAlreadyRunning,
)
from prewarm_worker_loop import PrewarmWorkerLoop
from runtime import InstanceRuntime
from runtime_store import RuntimeStore, VolumeAttachmentStore
from startup_store import StartupStore


DEFAULT_DATABASE = Path("/var/lib/fc/control-plane.db")
DEFAULT_STATE_ROOT = Path("/var/lib/fc")
DEFAULT_PROFILE_KEY = "default-2vcpu-1g"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Maintain the local Firecracker prewarm pool",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--prewarm-config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--profile-key", default=DEFAULT_PROFILE_KEY)
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--max-backoff", type=int, default=60)
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one capacity check and exit instead of starting the loop",
    )
    return parser


def run(
    args: argparse.Namespace,
    *,
    runtime_factory: Callable[..., InstanceRuntime] = InstanceRuntime,
    capacity_probe_factory: Callable[..., LinuxHostCapacityProbe] = LinuxHostCapacityProbe,
    lock_factory: Callable[[Path], PrewarmWorkerLock] = PrewarmWorkerLock,
    emit: Callable[[str], None] | None = None,
) -> int:
    output = emit or (lambda message: print(message, flush=True))
    lock = lock_factory(args.lock_file)
    try:
        lock.acquire()
    except WorkerAlreadyRunning:
        output(f"PREWARM_WORKER_ALREADY_RUNNING lock={args.lock_file}")
        return 2
    try:
        config = PrewarmConfig.from_file(args.prewarm_config)
        runtime = runtime_factory(
            state_root=args.state_root,
            state_database=args.database,
        )
        prewarm_store = PrewarmStore(args.database)
        startup_store = StartupStore(args.database)
        attachment_store = VolumeAttachmentStore(args.database)
        runtime_store = RuntimeStore(args.database)
        claim_auditor = PrewarmClaimAuditor(
            prewarm_store=prewarm_store,
            startup_store=startup_store,
            attachment_store=attachment_store,
            runtime_store=runtime_store,
            profile_key=args.profile_key,
        )
        claim_compensator = PreActivationClaimCompensator(
            prewarm_store=prewarm_store,
            startup_store=startup_store,
            attachment_store=attachment_store,
            runtime_store=runtime_store,
            runtime=runtime,
        )
        activation_compensator = ActivationTimeoutClaimCompensator(
            prewarm_store=prewarm_store,
            startup_store=startup_store,
            attachment_store=attachment_store,
            runtime_store=runtime_store,
            runtime=runtime,
        )
        commit_pending_compensator = CommitPendingClaimCompensator(
            prewarm_store=prewarm_store,
            startup_store=startup_store,
            attachment_store=attachment_store,
            runtime_store=runtime_store,
        )
        worker = PrewarmWorker(
            config,
            prewarm_store=prewarm_store,
            profile_key=args.profile_key,
            builder=runtime,
            capacity_probe=capacity_probe_factory(runtime.network_manager),
            claim_auditor=claim_auditor,
            claim_compensator=claim_compensator,
            activation_compensator=activation_compensator,
            commit_pending_compensator=commit_pending_compensator,
        )
        loop = PrewarmWorkerLoop(
            worker,
            interval_seconds=config.health_interval_seconds,
            max_backoff_seconds=args.max_backoff,
            config_loader=lambda: PrewarmConfig.from_file(args.prewarm_config),
            emit=output,
        )
        output(
            "PREWARM_WORKER_READY "
            f"enabled={int(config.enabled)} profile={args.profile_key} "
            f"target={config.target_ready} max={config.max_total} "
            f"interval={config.health_interval_seconds}s mode={'once' if args.once else 'loop'}"
        )
        if args.once:
            reconciliation = loop.reconcile_once()
            if not reconciliation.success:
                return 1
            cycle = loop.run_once()
            return 0 if cycle.success else 1

        stop_signal = threading.Event()

        def request_stop(_signum, _frame) -> None:
            stop_signal.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        loop.run(stop_signal)
        output("PREWARM_WORKER_STOPPED")
        return 0
    finally:
        lock.release()


def main() -> None:
    raise SystemExit(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
