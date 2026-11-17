#!/usr/bin/env python3
"""预热 Worker 单实例锁测试。"""

import os
import tempfile
import unittest
from pathlib import Path

from prewarm_worker_lock import PrewarmWorkerLock, WorkerAlreadyRunning


class PrewarmWorkerLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "worker.lock"

    def test_second_worker_cannot_acquire_held_lock(self) -> None:
        first = PrewarmWorkerLock(self.path)
        second = PrewarmWorkerLock(self.path)
        first.acquire()
        self.addCleanup(first.release)
        with self.assertRaises(WorkerAlreadyRunning):
            second.acquire()
        self.assertEqual(self.path.read_text(encoding="ascii"), f"{os.getpid()}\n")

    def test_lock_can_be_reacquired_after_release(self) -> None:
        first = PrewarmWorkerLock(self.path)
        second = PrewarmWorkerLock(self.path)
        first.acquire()
        first.release()
        second.acquire()
        second.release()

    def test_context_manager_releases_after_exception(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "inside"):
            with PrewarmWorkerLock(self.path):
                raise RuntimeError("inside")
        with PrewarmWorkerLock(self.path):
            pass

    def test_same_lock_object_cannot_acquire_twice(self) -> None:
        lock = PrewarmWorkerLock(self.path)
        lock.acquire()
        self.addCleanup(lock.release)
        with self.assertRaisesRegex(RuntimeError, "already acquired"):
            lock.acquire()

    def test_release_without_acquire_is_idempotent(self) -> None:
        PrewarmWorkerLock(self.path).release()


if __name__ == "__main__":
    unittest.main()
