"""同一 Host 上预热 Worker 的进程级单实例锁。"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


DEFAULT_LOCK_PATH = Path("/run/ai-agent-prewarm-worker.lock")


class WorkerAlreadyRunning(RuntimeError):
    """锁已由另一 Worker 进程持有。"""


class PrewarmWorkerLock:
    """使用 Linux ``flock`` 保证一个 Host 只有一个补池 Worker。"""

    def __init__(self, path: Path = DEFAULT_LOCK_PATH) -> None:
        self.path = path
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self._descriptor is not None:
            raise RuntimeError("prewarm Worker lock is already acquired by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise WorkerAlreadyRunning(
                f"another prewarm Worker holds {self.path}"
            ) from error
        try:
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
        except Exception:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise
        self._descriptor = descriptor

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "PrewarmWorkerLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.release()
