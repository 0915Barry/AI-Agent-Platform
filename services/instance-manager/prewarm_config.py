#!/usr/bin/env python3
"""预热池配置：单一来源，读取并校验 config/prewarm.env。

阶段 0 只提供配置解析与校验，不启动任何 VMM、TAP 或租约。``PREWARM_ENABLED=0``
时对现有系统零影响。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "prewarm.env"


class PrewarmConfigError(ValueError):
    """预热配置无效。"""


@dataclass(frozen=True)
class PrewarmConfig:
    enabled: bool
    target_ready: int
    max_total: int
    build_concurrency: int
    ttl_seconds: int
    health_interval_seconds: int
    claim_timeout_seconds: int
    activate_timeout_seconds: int
    min_available_mib: int

    @staticmethod
    def _parse_bool(raw: str, key: str) -> bool:
        if raw in {"1", "true", "True", "yes"}:
            return True
        if raw in {"0", "false", "False", "no", ""}:
            return False
        raise PrewarmConfigError(f"invalid {key}: {raw!r}")

    @staticmethod
    def _parse_int(raw: str, key: str) -> int:
        try:
            return int(raw)
        except ValueError as error:
            raise PrewarmConfigError(f"invalid {key}: {raw!r}") from error

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "PrewarmConfig":
        """从进程环境或给定映射读取配置，并做范围校验。"""
        values = dict(os.environ if env is None else env)

        enabled = cls._parse_bool(values.get("PREWARM_ENABLED", "0"), "PREWARM_ENABLED")
        target_ready = cls._parse_int(
            values.get("PREWARM_TARGET_READY", "1"), "PREWARM_TARGET_READY"
        )
        max_total = cls._parse_int(values.get("PREWARM_MAX_TOTAL", "2"), "PREWARM_MAX_TOTAL")
        build_concurrency = cls._parse_int(
            values.get("PREWARM_BUILD_CONCURRENCY", "1"), "PREWARM_BUILD_CONCURRENCY"
        )
        ttl_seconds = cls._parse_int(values.get("PREWARM_TTL_SECONDS", "900"), "PREWARM_TTL_SECONDS")
        health_interval_seconds = cls._parse_int(
            values.get("PREWARM_HEALTH_INTERVAL_SECONDS", "5"), "PREWARM_HEALTH_INTERVAL_SECONDS"
        )
        claim_timeout_seconds = cls._parse_int(
            values.get("PREWARM_CLAIM_TIMEOUT_SECONDS", "30"), "PREWARM_CLAIM_TIMEOUT_SECONDS"
        )
        activate_timeout_seconds = cls._parse_int(
            values.get("PREWARM_ACTIVATE_TIMEOUT_SECONDS", "10"), "PREWARM_ACTIVATE_TIMEOUT_SECONDS"
        )
        min_available_mib = cls._parse_int(
            values.get("PREWARM_MIN_AVAILABLE_MIB", "2048"), "PREWARM_MIN_AVAILABLE_MIB"
        )

        if target_ready < 0:
            raise PrewarmConfigError("PREWARM_TARGET_READY must be >= 0")
        if max_total < target_ready:
            raise PrewarmConfigError("PREWARM_MAX_TOTAL must be >= PREWARM_TARGET_READY")
        if build_concurrency < 1:
            raise PrewarmConfigError("PREWARM_BUILD_CONCURRENCY must be >= 1")
        for key, value in (
            ("PREWARM_TTL_SECONDS", ttl_seconds),
            ("PREWARM_HEALTH_INTERVAL_SECONDS", health_interval_seconds),
            ("PREWARM_CLAIM_TIMEOUT_SECONDS", claim_timeout_seconds),
            ("PREWARM_ACTIVATE_TIMEOUT_SECONDS", activate_timeout_seconds),
        ):
            if value < 1:
                raise PrewarmConfigError(f"{key} must be >= 1")
        if min_available_mib < 0:
            raise PrewarmConfigError("PREWARM_MIN_AVAILABLE_MIB must be >= 0")

        return cls(
            enabled=enabled,
            target_ready=target_ready,
            max_total=max_total,
            build_concurrency=build_concurrency,
            ttl_seconds=ttl_seconds,
            health_interval_seconds=health_interval_seconds,
            claim_timeout_seconds=claim_timeout_seconds,
            activate_timeout_seconds=activate_timeout_seconds,
            min_available_mib=min_available_mib,
        )

    @classmethod
    def from_file(cls, path: Path) -> "PrewarmConfig":
        """从 config/prewarm.env 风格的文件读取（忽略注释与空行）。"""
        env: dict[str, str] = {}
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise PrewarmConfigError(f"invalid line in {path}: {line!r}")
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
        return cls.from_env(env)

    def summary(self) -> str:
        return (
            f"enabled={int(self.enabled)} target={self.target_ready} max={self.max_total} "
            f"concurrency={self.build_concurrency} ttl={self.ttl_seconds}s "
            f"health={self.health_interval_seconds}s claim={self.claim_timeout_seconds}s "
            f"activate={self.activate_timeout_seconds}s min_mib={self.min_available_mib}"
        )


def main() -> None:
    config = PrewarmConfig.from_file(DEFAULT_CONFIG_PATH)
    print(f"PREWARM_STATUS {config.summary()}")


if __name__ == "__main__":
    main()
