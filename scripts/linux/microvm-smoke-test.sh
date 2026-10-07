#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi

config="/var/lib/fc/smoke/config.json"
console_log="/var/lib/fc/smoke/console.log"
api_socket="/var/lib/fc/smoke/firecracker.socket"

if [[ ! -x /usr/local/bin/firecracker ]]; then
  echo "Firecracker is not installed" >&2
  exit 1
fi

if [[ ! -f "${config}" ]]; then
  echo "Smoke-test configuration is missing; run prepare-microvm first" >&2
  exit 1
fi

rm -f "${console_log}" "${api_socket}"

echo "Starting the minimal Firecracker microVM..."
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

for _ in $(seq 1 50); do
  if [[ -S "${api_socket}" ]]; then
    break
  fi
  if ! kill -0 "${firecracker_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.1
done

if [[ -S "${api_socket}" ]]; then
  echo "Firecracker API state:"
  curl --silent --show-error --unix-socket "${api_socket}" http://localhost/ || true
  echo
else
  echo "Firecracker API socket was not created" >&2
fi

sleep 12

if grep -q 'Welcome to fcinitrd' "${console_log}"; then
  echo "PASS: Firecracker booted the $(uname -m) microVM and reached guest init"
  grep -E 'Boot took|Welcome to fcinitrd' "${console_log}" || true
  exit 0
fi

echo "FAIL: the guest did not reach its init process" >&2
echo "Console output:" >&2
tail -n 120 "${console_log}" >&2
exit 1
