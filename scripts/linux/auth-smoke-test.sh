#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"

# M16 不改动 guest/rootfs；这里直接回归密码派生、会话撤销、HTTP Cookie、
# 跨用户 404 隔离和实例配额，因此不需要重新构建 microVM 系统盘。
python3 -m unittest discover \
  -s "${repo_dir}/services/control-plane" \
  -p 'test_*.py'

echo "PASS: local authentication, session, ownership, and quota checks passed"
echo "M16_AUTH_READY password=pbkdf2-sha256 session=httponly:12h ownership=isolated quota=5:2 bind=loopback"
