#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi

config="/var/lib/fc/runtime/config.json"
console_log="/var/lib/fc/runtime/console.log"
api_socket="/var/lib/fc/runtime/firecracker.socket"

if [[ ! -x /usr/local/bin/firecracker ]]; then
  echo "Firecracker is not installed" >&2
  exit 1
fi
if [[ ! -f "${config}" ]]; then
  echo "Runtime configuration is missing; run build-rootfs first" >&2
  exit 1
fi

rm -f "${console_log}" "${api_socket}"
echo "Starting the Pi runtime microVM (60 second test window)..."
/usr/local/bin/firecracker \
  --api-sock "${api_socket}" \
  --config-file "${config}" \
  > "${console_log}" 2>&1 &
firecracker_pid=$!

cleanup() {
  if kill -0 "${firecracker_pid}" 2>/dev/null; then
    kill "${firecracker_pid}" 2>/dev/null || true
    sleep 1
    kill -9 "${firecracker_pid}" 2>/dev/null || true
  fi
  wait "${firecracker_pid}" 2>/dev/null || true
  rm -f "${api_socket}"
}
trap cleanup EXIT

for _ in $(seq 1 120); do
  if grep -q '^AGENT_RUNTIME_READY ' "${console_log}" 2>/dev/null; then
    echo "PASS: runtime rootfs booted with pinned Node, Pi Agent, and non-root pi user"
    grep '^AGENT_RUNTIME_READY ' "${console_log}"
    exit 0
  fi
  if ! kill -0 "${firecracker_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

echo "FAIL: runtime rootfs did not report readiness" >&2
echo "Console output:" >&2
tail -n 160 "${console_log}" >&2
exit 1
