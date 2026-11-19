#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"
state_root="/var/lib/fc/prewarm-concurrency"
worker_lock="/run/ai-agent-prewarm-worker.lock"
test_lock="/run/ai-agent-prewarm-concurrency-test.lock"
users="${1:-3}"
ready="${2:-1}"
rounds="${3:-1}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi
for value in "${users}" "${ready}" "${rounds}"; do
  if [[ ! "${value}" =~ ^[1-9][0-9]*$ ]]; then
    echo "users, ready, and rounds must be positive integers" >&2
    exit 1
  fi
done
if (( users < 2 || users > 10 )); then
  echo "users must be between 2 and 10" >&2
  exit 1
fi
if (( ready < 1 || ready >= users )); then
  echo "ready must be at least 1 and below users" >&2
  exit 1
fi
if (( rounds < 1 || rounds > 20 )); then
  echo "rounds must be between 1 and 20" >&2
  exit 1
fi
if "${repo_dir}/scripts/linux/control-plane-service.sh" status >/dev/null 2>&1; then
  echo "Stop the long-running control plane before prewarm-concurrency-test" >&2
  exit 1
fi
if ! flock -n "${worker_lock}" true 2>/dev/null; then
  echo "Stop the long-running prewarm worker before prewarm-concurrency-test (${worker_lock})" >&2
  exit 1
fi
exec 9>"${test_lock}"
if ! flock -n 9; then
  echo "A prewarm concurrency test is already running (${test_lock})" >&2
  exit 1
fi
if [[ -e "${state_root}" ]]; then
  echo "Refusing to overwrite stale test state: ${state_root}" >&2
  exit 1
fi
if [[ ! -f /srv/fc/artifacts/agent-rootfs.ext4 ]]; then
  echo "Runtime rootfs is missing; run build-rootfs first" >&2
  exit 1
fi

python3 "${repo_dir}/scripts/linux/prewarm-concurrency-smoke.py" \
  "${users}" "${ready}" "${rounds}"
