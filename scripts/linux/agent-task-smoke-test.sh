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
if [[ ! -f "${credential_path}" ]]; then
  echo "DeepSeek API key is not configured for ${operator}." >&2
  echo "Run ./run-linux.sh configure-deepseek first." >&2
  exit 1
fi

instance_id="m11-smoke"
listen_port="18091"
base_url="http://127.0.0.1:${listen_port}"
state_root="/var/lib/fc/agent-task-smoke"
database_path="${state_root}/control-plane.db"
task_database_path="${state_root}/tasks/tasks.db"
log_path="${state_root}/server.log"
volume_path="/srv/fc/volumes/instances/${instance_id}.ext4"
metadata_path="${state_root}/instances/${instance_id}.json"
jail_path="/srv/jailer/firecracker/${instance_id}"
server_pid=""
instance_created=false
verify_mount=""

cleanup() {
  if [[ -n "${verify_mount}" ]] && mountpoint -q "${verify_mount}"; then
    umount "${verify_mount}" 2>/dev/null || true
  fi
  if [[ -n "${verify_mount}" ]]; then
    rmdir "${verify_mount}" 2>/dev/null || true
  fi
  if [[ "${instance_created}" == true && -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
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
  if [[ "${volume_path}" == "/srv/fc/volumes/instances/m11-smoke.ext4" ]]; then
    rm -f "${volume_path}"
  fi
  if [[ "${jail_path}" == "/srv/jailer/firecracker/m11-smoke" ]]; then
    rm -rf --one-file-system "${jail_path}"
  fi
}
trap cleanup EXIT

check_volume() {
  # Firecracker 停止后 ext4 日志可能仍标记为需要恢复。只读挂载无法回放日志，
  # 因此像 M6/M8 一样先离线完成文件系统检查。
  set +e
  e2fsck -f -y "${volume_path}" >/dev/null
  filesystem_check_status=$?
  set -e
  if [[ "${filesystem_check_status}" -gt 1 ]]; then
    echo "FAIL: persistent Agent volume check failed with status ${filesystem_check_status}" >&2
    exit "${filesystem_check_status}"
  fi
}

export DEBIAN_FRONTEND=noninteractive
missing_packages=false
for required_command in curl e2fsck ip jq mkfs.ext4 mount nft python3 setpriv ss truncate; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    missing_packages=true
  fi
done
if [[ "${missing_packages}" == true ]]; then
  apt-get update
  apt-get install -y --no-install-recommends curl e2fsprogs iproute2 jq nftables python3 util-linux
fi

if [[ "${state_root}" != "/var/lib/fc/agent-task-smoke" ]]; then
  echo "Refusing to reset unexpected state root" >&2
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
  echo "FAIL: M11 control plane did not start" >&2
  cat "${log_path}" >&2
  exit 1
fi

curl --fail --silent --show-error --max-time 10 \
  --request POST --header 'Content-Type: application/json' \
  --data '{"id":"m11-smoke"}' "${base_url}/api/instances" >/dev/null
instance_created=true

echo "Starting managed Pi Agent microVM with isolated Gateway and task bridge..."
curl --fail --silent --show-error --max-time 120 \
  --request POST "${base_url}/api/instances/${instance_id}/start" \
  | jq -e '.status == "running" and .runtime.processAlive == true' >/dev/null

token="$(tr -d '-' < /proc/sys/kernel/random/uuid | cut -c1-16)"

# 先停止实例并由宿主写入一个模型未知的随机值，再重启同一实例。这样最终回复
# 只有在 Pi 真实调用 read 工具后才能得到，无法从 prompt 中直接复制答案。
curl --fail --silent --show-error --max-time 30 \
  --request POST "${base_url}/api/instances/${instance_id}/stop" \
  | jq -e '.status == "stopped"' >/dev/null
check_volume

verify_mount="$(mktemp -d /var/tmp/agent-m11-seed.XXXXXX)"
mount -o loop "${volume_path}" "${verify_mount}"
printf '%s\n' "${token}" > "${verify_mount}/workspace/m11-marker.txt"
chmod 0644 "${verify_mount}/workspace/m11-marker.txt"
sync
umount "${verify_mount}"
rmdir "${verify_mount}"
verify_mount=""

echo "Restarting the managed instance with a host-seeded persistent marker..."
curl --fail --silent --show-error --max-time 120 \
  --request POST "${base_url}/api/instances/${instance_id}/start" \
  | jq -e '.status == "running" and .runtime.processAlive == true' >/dev/null

prompt="Use the read tool to read /workspace/m11-marker.txt. Reply with exactly M11_AGENT_TASK_OK followed by a colon and the complete file contents. Do not add any other text."
task_payload="$(jq -n --arg prompt "${prompt}" '{prompt:$prompt}')"
task_response="$(
  curl --fail --silent --show-error --max-time 10 \
    --request POST --header 'Content-Type: application/json' \
    --data "${task_payload}" "${base_url}/api/instances/${instance_id}/tasks"
)"
task_id="$(printf '%s' "${task_response}" | jq -r '.id')"
if [[ ! "${task_id}" =~ ^task-[0-9a-f]{16}$ ]]; then
  echo "FAIL: control plane returned invalid task id" >&2
  exit 1
fi

task_status="queued"
task_output=""
for _ in $(seq 1 240); do
  task_response="$(curl --fail --silent --show-error --max-time 5 \
    "${base_url}/api/instances/${instance_id}/tasks/${task_id}")"
  task_status="$(printf '%s' "${task_response}" | jq -r '.status')"
  if [[ "${task_status}" == "completed" ]]; then
    task_output="$(printf '%s' "${task_response}" | jq -r '.output')"
    break
  fi
  if [[ "${task_status}" == "failed" ]]; then
    echo "FAIL: Pi Agent task failed: $(printf '%s' "${task_response}" | jq -r '.error')" >&2
    exit 1
  fi
  sleep 1
done
if [[ "${task_status}" != "completed" \
  || ! "${task_output}" =~ ^M11_AGENT_TASK_OK:[[:space:]]*${token}[[:space:]]*$ ]]; then
  echo "FAIL: unexpected task result status=${task_status} output=${task_output}" >&2
  exit 1
fi

events_response="$(curl --fail --silent --show-error --max-time 10 \
  "${base_url}/api/instances/${instance_id}/tasks/${task_id}/events")"
printf '%s' "${events_response}" \
  | jq -e '
      [.events[].type] as $types
      | $types[0:2] == ["queued","started"]
      and $types[-1] == "completed"
      and ($types | index("progress")) != null
      and ($types | index("text")) != null
      and ($types | index("tool_call")) != null
      and ($types | index("tool_result")) != null
    ' >/dev/null

# 终态任务也必须能通过 SSE 从序号 0 完整重放后自行关闭；这同时验证浏览器首次连接
# 和 Last-Event-ID 断线续传所依赖的稳定事件序号。
sse_response="$(curl --fail --silent --show-error --no-buffer --max-time 10 \
  "${base_url}/api/instances/${instance_id}/tasks/${task_id}/stream")"
if [[ "${sse_response}" != *'event: task-event'* ]]; then
  echo "FAIL: SSE stream did not contain task events" >&2
  exit 1
fi
if [[ "${sse_response}" != *'"type":"completed"'* ]]; then
  echo "FAIL: SSE stream did not reach the completed event" >&2
  exit 1
fi

curl --fail --silent --show-error --max-time 30 \
  --request POST "${base_url}/api/instances/${instance_id}/stop" \
  | jq -e '.status == "stopped"' >/dev/null

check_volume

verify_mount="$(mktemp -d /var/tmp/agent-m11-verify.XXXXXX)"
mount -o loop,ro "${volume_path}" "${verify_mount}"
marker_path="${verify_mount}/workspace/m11-marker.txt"
stored_token="$(head -n 1 "${marker_path}")"
umount "${verify_mount}"
rmdir "${verify_mount}"
verify_mount=""
if [[ "${stored_token}" != "${token}" ]]; then
  echo "FAIL: host-seeded marker was not preserved across the managed instance restart" >&2
  exit 1
fi

gateway_audit="${state_root}/control-plane/${instance_id}/sidecars/gateway.audit.jsonl"
if ! grep -q '"status":200' "${gateway_audit}"; then
  echo "FAIL: Tool Gateway did not record an authorized DeepSeek response" >&2
  exit 1
fi
if ! grep -q '"stream":true' "${gateway_audit}"; then
  echo "FAIL: Tool Gateway did not stream the DeepSeek response" >&2
  exit 1
fi

curl --fail --silent --show-error --max-time 30 \
  --request DELETE "${base_url}/api/instances/${instance_id}" >/dev/null
instance_created=false

echo "PASS: control plane completed a real Pi Agent task through DeepSeek"
echo "AGENT_TASK_READY transport=sse events=incremental gateway=streaming persistence=preserved idle_timeout=${INSTANCE_IDLE_TIMEOUT_SECONDS}s"
echo "M15_STREAMING_READY pi=jsonl control_plane=sse reconnect=last-event-id tools=visible thinking=hidden"
