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

instance_id="pi-gateway-smoke"
listen_port="18091"
base_url="http://127.0.0.1:${listen_port}"
state_root="/var/lib/fc/pi-gateway-smoke"
database_path="${state_root}/control-plane.db"
task_database_path="${state_root}/tasks/tasks.db"
log_path="${state_root}/server.log"
metadata_path="${state_root}/instances/${instance_id}.json"
volume_path="/srv/fc/volumes/instances/${instance_id}.ext4"
jail_path="/srv/jailer/firecracker/${instance_id}"
server_pid=""
instance_created=false

cleanup() {
  if [[ "${instance_created}" == true && -n "${server_pid}" ]] \
    && kill -0 "${server_pid}" 2>/dev/null; then
    curl --silent --max-time 20 --request DELETE \
      "${base_url}/api/instances/${instance_id}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
  fi
  if [[ -f "${metadata_path}" ]]; then
    python3 "${repo_dir}/services/instance-manager/lifecycle.py" \
      --state-root "${state_root}" destroy "${instance_id}" >/dev/null 2>&1 || true
  fi
  rm -f "${volume_path}"
  rm -rf --one-file-system "${jail_path}"
}
trap cleanup EXIT

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
if [[ "${state_root}" != "/var/lib/fc/pi-gateway-smoke" ]]; then
  echo "Refusing to reset unexpected state root: ${state_root}" >&2
  exit 1
fi
if [[ -f "${metadata_path}" ]]; then
  python3 "${repo_dir}/services/instance-manager/lifecycle.py" \
    --state-root "${state_root}" destroy "${instance_id}" >/dev/null 2>&1 || true
fi
rm -rf --one-file-system "${state_root}"
rm -f "${volume_path}"
install -d -o root -g root -m 0755 "${state_root}"

python3 "${repo_dir}/services/control-plane/server.py" \
  --listen-host 127.0.0.1 \
  --listen-port "${listen_port}" \
  --database "${database_path}" \
  --task-database "${task_database_path}" \
  --deepseek-credential "${credential_path}" \
  --state-root "${state_root}" \
  --idle-timeout "${INSTANCE_IDLE_TIMEOUT_SECONDS}" \
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
  echo "FAIL: Pi Gateway smoke control plane did not start" >&2
  cat "${log_path}" >&2
  exit 1
fi

curl --fail --silent --show-error --max-time 10 \
  --request POST --header 'Content-Type: application/json' \
  --data '{"id":"pi-gateway-smoke"}' "${base_url}/api/instances" >/dev/null
instance_created=true

echo "Starting a managed microVM and submitting one task through the host Pi Gateway..."
start_response="$(
  curl --fail --silent --show-error --max-time 120 \
    --request POST "${base_url}/api/instances/${instance_id}/start"
)"
printf '%s' "${start_response}" \
  | jq -e '.status == "running" and .runtime.processAlive == true and .runtime.rpcPort == 8080' \
  >/dev/null
guest_ip="$(printf '%s' "${start_response}" | jq -r '.runtime.guestIp')"

verification_token="PI_GATEWAY_OK_$(tr -d '-' < /proc/sys/kernel/random/uuid | cut -c1-12)"
prompt="请只回复 ${verification_token}，不要添加任何其他内容。"
task_response="$(
  jq -n --arg prompt "${prompt}" '{prompt:$prompt}' \
    | curl --fail --silent --show-error --max-time 10 \
      --request POST --header 'Content-Type: application/json' \
      --data-binary @- "${base_url}/api/instances/${instance_id}/tasks"
)"
task_id="$(printf '%s' "${task_response}" | jq -r '.id')"

task_status="queued"
task_output=""
for _ in $(seq 1 300); do
  task_response="$(
    curl --fail --silent --show-error --max-time 5 \
      "${base_url}/api/instances/${instance_id}/tasks/${task_id}"
  )"
  task_status="$(printf '%s' "${task_response}" | jq -r '.status')"
  if [[ "${task_status}" == "completed" ]]; then
    task_output="$(printf '%s' "${task_response}" | jq -r '.output')"
    break
  fi
  if [[ "${task_status}" == "failed" ]]; then
    echo "FAIL: Pi Gateway task failed" >&2
    printf '%s\n' "${task_response}" | jq . >&2
    tail -n 120 "${log_path}" >&2
    exit 1
  fi
  sleep 1
done
if [[ "${task_status}" != "completed" || "${task_output}" != "${verification_token}" ]]; then
  echo "FAIL: unexpected Pi Gateway result status=${task_status} output=${task_output}" >&2
  tail -n 120 "${log_path}" >&2
  exit 1
fi

events="$(
  curl --fail --silent --show-error --max-time 10 \
    "${base_url}/api/instances/${instance_id}/tasks/${task_id}/events"
)"
printf '%s' "${events}" | jq -e '
  [.events[].type] as $types
  | ($types | index("queued")) != null
  and ($types | index("started")) != null
  and ($types | index("progress")) != null
  and ($types | index("text")) != null
  and ($types | index("completed")) != null
' >/dev/null

curl --fail --silent --show-error --max-time 30 \
  --request POST "${base_url}/api/instances/${instance_id}/stop" \
  | jq -e '.status == "stopped" and .runtime.processAlive == false' >/dev/null
curl --fail --silent --show-error --max-time 30 \
  --request DELETE "${base_url}/api/instances/${instance_id}" >/dev/null
instance_created=false

echo "PASS: host Pi Gateway dispatched a real task to microVM Pi RPC"
echo "PI_GATEWAY_READY guest=${guest_ip}:8080 transport=tcp protocol=jsonl dispatch=host events=streamed bridge=absent"
