#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"
state_root="/var/lib/fc/prewarm-pressure"
worker_lock="/run/ai-agent-prewarm-worker.lock"
pressure_lock="/run/ai-agent-prewarm-pressure.lock"
cycles="${1:-100}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi
if [[ ! "${cycles}" =~ ^[1-9][0-9]*$ ]] || (( cycles > 1000 )); then
  echo "Cycle count must be an integer between 1 and 1000" >&2
  exit 1
fi
if "${repo_dir}/scripts/linux/control-plane-service.sh" status >/dev/null 2>&1; then
  echo "Stop the long-running control plane before prewarm-pressure-test" >&2
  exit 1
fi
if ! flock -n "${worker_lock}" true 2>/dev/null; then
  echo "Stop the long-running prewarm worker before prewarm-pressure-test (${worker_lock})" >&2
  exit 1
fi
if ! flock -n "${pressure_lock}" true 2>/dev/null; then
  echo "A prewarm pressure test is already running (${pressure_lock})" >&2
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

python3 "${repo_dir}/scripts/linux/prewarm-pressure-smoke.py" "${cycles}"
