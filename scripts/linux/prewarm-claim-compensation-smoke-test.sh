#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"
state_root="/var/lib/fc/prewarm-claim-compensation-smoke"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi
if "${repo_dir}/scripts/linux/control-plane-service.sh" status >/dev/null 2>&1; then
  echo "Stop the long-running control plane before prewarm-claim-compensation-smoke-test" >&2
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

python3 "${repo_dir}/scripts/linux/prewarm-claim-compensation-smoke.py"
