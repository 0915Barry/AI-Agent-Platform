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
  echo "Run ./run-linux.sh configure-deepseek first." >&2
  exit 1
fi

# 测试使用独立租约账本，不能与正式控制面同时分配地址。
if "${repo_dir}/scripts/linux/control-plane-service.sh" status >/dev/null 2>&1; then
  echo "Stop the long-running control plane before recovery-smoke-test: ./run-linux.sh stop" >&2
  exit 1
fi

for required_command in curl ip jq nft python3; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "Required command is missing: ${required_command}" >&2
    exit 1
  fi
done
if [[ ! -f /srv/fc/artifacts/agent-rootfs.ext4 ]]; then
  echo "Runtime rootfs is missing; run build-rootfs first" >&2
  exit 1
fi

listen_port="18094"
base_url="http://127.0.0.1:${listen_port}"
state_root="/var/lib/fc/recovery-smoke"
database_path="${state_root}/control-plane.db"
task_database_path="${state_root}/tasks/tasks.db"
log_path="${state_root}/server.log"
suffix="$(tr -d '-' < /proc/sys/kernel/random/uuid | cut -c1-10)"
instance_id="recovery-${suffix}"
metadata_path="${state_root}/instances/${instance_id}.json"
volume_path="/srv/fc/volumes/instances/${instance_id}.ext4"
jail_path="/srv/jailer/firecracker/${instance_id}"
server_pid=""
instance_created=false

stop_server() {
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill "${server_pid}"
    wait "${server_pid}" 2>/dev/null || true
  fi
  server_pid=""
}

start_server() {
  python3 "${repo_dir}/services/control-plane/server.py" \
    --listen-host 127.0.0.1 \
    --listen-port "${listen_port}" \
    --database "${database_path}" \
    --task-database "${task_database_path}" \
    --deepseek-credential "${credential_path}" \
    --state-root "${state_root}" \
    --idle-timeout "${INSTANCE_IDLE_TIMEOUT_SECONDS}" \
    >> "${log_path}" 2>&1 &
  server_pid=$!

  local ready=false
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
    echo "FAIL: recovery smoke control plane did not start" >&2
    cat "${log_path}" >&2
    exit 1
  fi
}

cleanup() {
  if [[ "${instance_created}" == true && -n "${server_pid}" ]] \
    && kill -0 "${server_pid}" 2>/dev/null; then
    curl --silent --max-time 30 --request DELETE \
      "${base_url}/api/instances/${instance_id}" >/dev/null 2>&1 || true
  fi
  stop_server
  if [[ -f "${metadata_path}" ]]; then
    python3 "${repo_dir}/services/instance-manager/lifecycle.py" \
      --state-root "${state_root}" destroy "${instance_id}" >/dev/null 2>&1 || true
  fi
  rm -f "${volume_path}"
  rm -rf --one-file-system "${jail_path}"
}
trap cleanup EXIT

if [[ "${state_root}" != "/var/lib/fc/recovery-smoke" ]]; then
  echo "Refusing to reset unexpected state root: ${state_root}" >&2
  exit 1
fi
rm -rf --one-file-system "${state_root}"
install -d -o root -g root -m 0755 "${state_root}"
start_server

curl --fail --silent --show-error --max-time 10 \
  --request POST --header 'Content-Type: application/json' \
  --data "{\"id\":\"${instance_id}\"}" "${base_url}/api/instances" >/dev/null
instance_created=true

echo "Starting one managed microVM for recovery checks..."
start_response="$(
  curl --fail --silent --show-error --max-time 120 \
    --request POST "${base_url}/api/instances/${instance_id}/start"
)"
printf '%s' "${start_response}" \
  | jq -e '.status == "running" and .runtime.processAlive == true' >/dev/null
firecracker_pid="$(printf '%s' "${start_response}" | jq -r '.runtime.pid')"
tap_name="$(printf '%s' "${start_response}" | jq -r '.runtime.tapName')"
nft_family="$(printf '%s' "${start_response}" | jq -r '.runtime.nftFamily')"
nft_table="$(printf '%s' "${start_response}" | jq -r '.runtime.nftTable')"
volume_inode="$(stat -c '%i' "${volume_path}")"

echo "Simulating a Control Plane process restart..."
stop_server
if ! kill -0 "${firecracker_pid}" 2>/dev/null; then
  echo "FAIL: Firecracker stopped with the Control Plane" >&2
  exit 1
fi
start_server
restarted_response="$(
  curl --fail --silent --show-error --max-time 10 \
    "${base_url}/api/instances/${instance_id}"
)"
printf '%s' "${restarted_response}" \
  | jq -e '.status == "running" and .runtime.processAlive == true' >/dev/null

echo "Simulating an unexpected Firecracker process exit..."
kill -KILL "${firecracker_pid}"
reaped=false
for _ in $(seq 1 60); do
  metadata_state="$(jq -r '.status // ""' "${metadata_path}" 2>/dev/null || true)"
  lease_present=false
  if [[ -f "${state_root}/network-leases.json" ]] \
    && jq -e --arg id "${instance_id}" '.leases[$id] != null' \
      "${state_root}/network-leases.json" >/dev/null 2>&1; then
    lease_present=true
  fi
  if [[ "${metadata_state}" == "stopped" && "${lease_present}" == false \
    && ! -e "${jail_path}" ]]; then
    reaped=true
    break
  fi
  sleep 0.25
done
if [[ "${reaped}" != true ]]; then
  echo "FAIL: watcher did not clean the crashed Firecracker instance" >&2
  tail -n 120 "${log_path}" >&2
  exit 1
fi
if ip link show "${tap_name}" >/dev/null 2>&1; then
  echo "FAIL: TAP remained after Firecracker crash: ${tap_name}" >&2
  exit 1
fi
if nft list table "${nft_family}" "${nft_table}" >/dev/null 2>&1; then
  echo "FAIL: nftables table remained after Firecracker crash: ${nft_table}" >&2
  exit 1
fi
if [[ ! -f "${volume_path}" || "$(stat -c '%i' "${volume_path}")" != "${volume_inode}" ]]; then
  echo "FAIL: persistent data disk was lost or replaced after Firecracker crash" >&2
  exit 1
fi

# GET 触发 Control Plane 使用真实运行时状态校准 SQLite。
reconciled_response="$(
  curl --fail --silent --show-error --max-time 10 \
    "${base_url}/api/instances/${instance_id}"
)"
printf '%s' "${reconciled_response}" | jq -e '.status == "stopped"' >/dev/null
if [[ "$(jq -r '.stopReason' "${metadata_path}")" != "process_exited" ]]; then
  echo "FAIL: lifecycle metadata did not record process_exited" >&2
  exit 1
fi

echo "Restarting the reconciled instance with its preserved data disk..."
recovery_start="$(
  curl --fail --silent --show-error --max-time 120 \
    --request POST "${base_url}/api/instances/${instance_id}/start"
)"
printf '%s' "${recovery_start}" \
  | jq -e '.status == "running" and .runtime.processAlive == true' >/dev/null
if [[ "$(stat -c '%i' "${volume_path}")" != "${volume_inode}" ]]; then
  echo "FAIL: restart did not reuse the preserved data disk" >&2
  exit 1
fi

curl --fail --silent --show-error --max-time 30 \
  --request POST "${base_url}/api/instances/${instance_id}/stop" \
  | jq -e '.status == "stopped"' >/dev/null
curl --fail --silent --show-error --max-time 30 \
  --request DELETE "${base_url}/api/instances/${instance_id}" >/dev/null
instance_created=false

if [[ -e "${volume_path}" || -e "${jail_path}" || -e "${metadata_path}" ]]; then
  echo "FAIL: resources remained after final destroy" >&2
  exit 1
fi
if [[ -f "${state_root}/network-leases.json" ]] \
  && ! jq -e '.version == 1 and (.leases | length == 0)' \
    "${state_root}/network-leases.json" >/dev/null; then
  echo "FAIL: network lease remained after final destroy" >&2
  exit 1
fi

stop_server
rm -rf --one-file-system "${state_root}"

echo "PASS: Control Plane restart and Firecracker crash recovery preserved instance data"
echo "RECOVERY_READY control_plane=reconnected firecracker_crash=reaped state=reconciled network=clean data=preserved restart=verified"
