#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"

# shellcheck disable=SC1091
. "${repo_dir}/config/lifecycle.env"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi
operator="${SUDO_USER:-}"
if [[ -z "${operator}" || "${operator}" == "root" ]]; then
  echo "Run this test as a configured normal user through sudo" >&2
  exit 1
fi
operator_home="$(getent passwd "${operator}" | cut -d: -f6)"
credential_path="${operator_home}/.config/ai-agent-platform/deepseek-api-key"
if [[ ! -s "${credential_path}" ]]; then
  echo "DeepSeek API key is not configured for ${operator}." >&2
  exit 1
fi

if "${repo_dir}/scripts/linux/control-plane-service.sh" status >/dev/null 2>&1; then
  echo "Stop the long-running control plane before auto-snapshot-smoke-test: ./run-linux.sh stop" >&2
  exit 1
fi
if [[ ! -f /srv/fc/artifacts/agent-rootfs.ext4 ]]; then
  echo "Runtime rootfs is missing; run build-rootfs first" >&2
  exit 1
fi

listen_port="18094"
base_url="http://127.0.0.1:${listen_port}"
state_root="/var/lib/fc/auto-snapshot-smoke"
database_path="${state_root}/control-plane.db"
state_file="${state_root}/agentctl-session.json"
log_path="${state_root}/server.log"
suffix="$(tr -d '-' < /proc/sys/kernel/random/uuid | cut -c1-10)"
instance_id="auto-snapshot-${suffix}"
username="auto-snapshot-${suffix}"
password="Snap-${suffix}-$(tr -d '-' < /proc/sys/kernel/random/uuid | cut -c1-12)"
server_pid=""
instance_created=false
volume_path=""
snapshot_path="/srv/fc/snapshots/instances/${instance_id}"

cli=(
  "${repo_dir}/agentctl"
  --base-url "${base_url}"
  --state-file "${state_file}"
  --no-color
)

cleanup() {
  if [[ "${instance_created}" == true && -n "${server_pid}" ]] \
    && kill -0 "${server_pid}" 2>/dev/null; then
    "${cli[@]}" destroy "${instance_id}" --yes >/dev/null 2>&1 || true
  fi
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
  fi
  python3 "${repo_dir}/services/instance-manager/lifecycle.py" \
    --state-root "${state_root}" --database "${database_path}" \
    destroy "${instance_id}" >/dev/null 2>&1 || true
  if [[ -n "${volume_path}" ]]; then
    rm -f "${volume_path}"
  fi
  rm -rf --one-file-system "/srv/jailer/firecracker/${instance_id}"
  rm -rf --one-file-system "${snapshot_path}"
  unset password
}
trap cleanup EXIT

if [[ "${state_root}" != "/var/lib/fc/auto-snapshot-smoke" ]]; then
  echo "Refusing to reset unexpected state root: ${state_root}" >&2
  exit 1
fi
rm -rf --one-file-system "${state_root}"
install -d -o root -g root -m 0755 "${state_root}"

printf '%s\n' "${password}" | env \
  PYTHONPATH="${repo_dir}/services/control-plane" \
  python3 -c '
import sys
from pathlib import Path
from auth import AuthStore
password_value = sys.stdin.readline().rstrip("\r\n")
AuthStore(Path(sys.argv[1])).create_user(sys.argv[2], password_value, claim_unowned=False)
' "${database_path}" "${username}"
user_id="$(python3 - "${database_path}" "${username}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    row = connection.execute("SELECT id FROM users WHERE username=?", (sys.argv[2],)).fetchone()
assert row is not None
print(row[0])
PY
)"
volume_path="/srv/fc/volumes/users/${user_id}.ext4"

python3 "${repo_dir}/services/control-plane/server.py" \
  --listen-host 127.0.0.1 \
  --listen-port "${listen_port}" \
  --database "${database_path}" \
  --deepseek-credential "${credential_path}" \
  --state-root "${state_root}" \
  --idle-timeout "${AUTO_SNAPSHOT_SMOKE_IDLE_TIMEOUT_SECONDS}" \
  --require-auth \
  > "${log_path}" 2>&1 &
server_pid=$!

ready=false
for _ in $(seq 1 40); do
  if curl --fail --silent --max-time 1 "${base_url}/healthz" >/dev/null 2>&1; then
    ready=true
    break
  fi
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.25
done
if [[ "${ready}" != true ]]; then
  echo "FAIL: auto-snapshot control plane did not start" >&2
  cat "${log_path}" >&2
  exit 1
fi

printf '%s\n' "${password}" | "${cli[@]}" login "${username}" --password-stdin >/dev/null
"${cli[@]}" create "${instance_id}" >/dev/null
instance_created=true
"${cli[@]}" start "${instance_id}" >/dev/null

echo "Waiting for the idle monitor to auto-snapshot the owned instance..."
suspended=false
for _ in $(seq 1 60); do
  if "${cli[@]}" status "${instance_id}" 2>/dev/null | grep -Fq '状态=suspended'; then
    suspended=true
    break
  fi
  sleep 0.5
done
if [[ "${suspended}" != true ]]; then
  echo "FAIL: owned instance was not auto-suspended after idle" >&2
  tail -n 80 "${log_path}" >&2
  exit 1
fi
if [[ ! -f "${snapshot_path}/manifest.json" ]]; then
  echo "FAIL: auto-suspend did not create a snapshot manifest" >&2
  exit 1
fi
echo "AUTO_SNAPSHOT_CREATED id=${instance_id} snapshot=present"

echo "Waiting for the suspended instance to be stopped after a second idle period..."
stopped=false
for _ in $(seq 1 60); do
  if "${cli[@]}" status "${instance_id}" 2>/dev/null | grep -Fq '状态=stopped'; then
    stopped=true
    break
  fi
  sleep 0.5
done
if [[ "${stopped}" != true ]]; then
  echo "FAIL: suspended instance was not stopped after the second idle period" >&2
  tail -n 80 "${log_path}" >&2
  exit 1
fi
if [[ -e "${snapshot_path}" ]]; then
  echo "FAIL: snapshot was not discarded on suspended-idle stop" >&2
  exit 1
fi
if [[ ! -e "${volume_path}" ]]; then
  echo "FAIL: user data volume was deleted during auto lifecycle" >&2
  exit 1
fi

echo "PASS: owned instance auto-snapshotted on idle, then stopped after a second idle period"
echo "AUTO_SNAPSHOT_READY idle=${AUTO_SNAPSHOT_SMOKE_IDLE_TIMEOUT_SECONDS}s snapshot=created discarded=on-stop volume=preserved"
