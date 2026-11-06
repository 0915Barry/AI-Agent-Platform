#!/usr/bin/env python3
"""预热池配置与接口的纯本地单元测试（阶段 0）。"""

import unittest

from prewarm_config import PrewarmConfig, PrewarmConfigError
from prewarm_manager import PrewarmManager


class PrewarmConfigTests(unittest.TestCase):
    def test_defaults_are_disabled(self) -> None:
        config = PrewarmConfig.from_env({})
        self.assertFalse(config.enabled)
        self.assertEqual(config.target_ready, 1)
        self.assertEqual(config.max_total, 2)
        self.assertEqual(config.ttl_seconds, 900)

    def test_parses_enabled_values(self) -> None:
        self.assertTrue(PrewarmConfig.from_env({"PREWARM_ENABLED": "1"}).enabled)
        self.assertFalse(PrewarmConfig.from_env({"PREWARM_ENABLED": "0"}).enabled)
        self.assertTrue(PrewarmConfig.from_env({"PREWARM_ENABLED": "true"}).enabled)

    def test_rejects_invalid_int(self) -> None:
        with self.assertRaises(PrewarmConfigError):
            PrewarmConfig.from_env({"PREWARM_TARGET_READY": "abc"})

    def test_rejects_negative_target(self) -> None:
        with self.assertRaises(PrewarmConfigError):
            PrewarmConfig.from_env({"PREWARM_TARGET_READY": "-1"})

    def test_rejects_max_below_target(self) -> None:
        with self.assertRaises(PrewarmConfigError):
            PrewarmConfig.from_env({"PREWARM_TARGET_READY": "3", "PREWARM_MAX_TOTAL": "2"})

    def test_rejects_invalid_enabled(self) -> None:
        with self.assertRaises(PrewarmConfigError):
            PrewarmConfig.from_env({"PREWARM_ENABLED": "maybe"})

    def test_rejects_non_positive_timeout(self) -> None:
        with self.assertRaises(PrewarmConfigError):
            PrewarmConfig.from_env({"PREWARM_TTL_SECONDS": "0"})


class PrewarmManagerTests(unittest.TestCase):
    def test_target_ready_reads_config(self) -> None:
        config = PrewarmConfig.from_env({"PREWARM_TARGET_READY": "5", "PREWARM_MAX_TOTAL": "5"})
        manager = PrewarmManager(config)
        self.assertEqual(manager.target_ready(), 5)

    def test_current_ready_is_zero_in_phase_zero(self) -> None:
        manager = PrewarmManager(PrewarmConfig.from_env({}))
        self.assertEqual(manager.current_ready(), 0)

    def test_unimplemented_methods_raise(self) -> None:
        manager = PrewarmManager(PrewarmConfig.from_env({}))
        with self.assertRaises(NotImplementedError):
            manager.fill_if_needed()
        with self.assertRaises(NotImplementedError):
            manager.claim(None, "agent-1", "user-1", "/tmp/x.ext4")


if __name__ == "__main__":
    unittest.main()
