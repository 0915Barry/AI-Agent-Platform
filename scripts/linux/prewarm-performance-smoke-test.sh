#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"
state_root="/var/lib/fc/prewarm-performance"
worker_lock="/run/ai-agent-prewarm-worker.lock"
perf_lock="/run/ai-agent-prewarm-performance.lock"
samples="${1:-30}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi
if [[ ! "${samples}" =~ ^[1-9][0-9]*$ ]] || (( samples > 200 )); then
  echo "Sample count must be an integer between 1 and 200" >&2
  exit 1
fi
if "${repo_dir}/scripts/linux/control-plane-service.sh" status >/dev/null 2>&1; then
  echo "Stop the long-running control plane before prewarm-performance-test" >&2
  exit 1
fi
if ! flock -n "${worker_lock}" true 2>/dev/null; then
  echo "Stop the long-running prewarm worker before prewarm-performance-test (${worker_lock})" >&2
  exit 1
fi
if ! flock -n "${perf_lock}" true 2>/dev/null; then
  echo "A prewarm performance test is already running (${perf_lock})" >&2
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

python3 "${repo_dir}/scripts/linux/prewarm-performance-smoke.py" "${samples}"
