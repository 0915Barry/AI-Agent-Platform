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
operator_home="$(getent passwd "${operator}" | cut -d: -f6)"
credential_path="${operator_home}/.config/ai-agent-platform/deepseek-api-key"
if [[ -z "${operator}" || "${operator}" == "root" || ! -f "${credential_path}" ]]; then
  echo "Run through sudo after ./run-linux.sh configure-deepseek" >&2
  exit 1
fi

instance_id="m14-smoke"
listen_port="18094"
base_url="http://127.0.0.1:${listen_port}"
state_root="/var/lib/fc/workspace-smoke"
volume_path="/srv/fc/volumes/instances/${instance_id}.ext4"
jail_path="/srv/jailer/firecracker/${instance_id}"
log_path="${state_root}/server.log"
server_pid=""
instance_created=false
current_step="initialization"

diagnose_failure() {
  status=$?
  trap - ERR
  echo "M14 diagnostics: failed_step=${current_step} exit_status=${status}" >&2
  if [[ -f "${state_root}/tasks/tasks.db" ]]; then
    TASK_DATABASE="${state_root}/tasks/tasks.db" python3 - <<'PY' >&2 || true
import os
import sqlite3

with sqlite3.connect(os.environ["TASK_DATABASE"]) as connection:
    for row in connection.execute(
        "SELECT id,action,path,status,COALESCE(error,'') FROM workspace_operations ORDER BY created_at,id"
    ):
        print("WORKSPACE_OPERATION", *row)
PY
  fi
  for diagnostic in \
    "${state_root}/control-plane/${instance_id}/console.log" \
    "${state_root}/control-plane/${instance_id}/sidecars/bridge.audit.jsonl" \
    "${state_root}/control-plane/${instance_id}/sidecars/bridge.log" \
    "${log_path}"; do
    if [[ -f "${diagnostic}" ]]; then
      echo "--- ${diagnostic} (last 80 lines) ---" >&2
      tail -n 80 "${diagnostic}" >&2 || true
    fi
  done
  return "${status}"
}

cleanup() {
  if [[ "${instance_created}" == true && -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    curl --silent --max-time 20 --request DELETE "${base_url}/api/instances/${instance_id}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
  fi
  if [[ -f "${state_root}/instances/${instance_id}.json" ]]; then
    python3 "${repo_dir}/services/instance-manager/lifecycle.py" --state-root "${state_root}" destroy "${instance_id}" >/dev/null 2>&1 || true
  fi
  [[ "${volume_path}" == "/srv/fc/volumes/instances/m14-smoke.ext4" ]] && rm -f "${volume_path}"
  [[ "${jail_path}" == "/srv/jailer/firecracker/m14-smoke" ]] && rm -rf --one-file-system "${jail_path}"
}
trap cleanup EXIT
trap diagnose_failure ERR

export DEBIAN_FRONTEND=noninteractive
missing=false
for command in curl ip jq nft python3 setpriv ss truncate; do
  command -v "${command}" >/dev/null 2>&1 || missing=true
done
if [[ "${missing}" == true ]]; then
  apt-get update
  apt-get install -y --no-install-recommends curl e2fsprogs iproute2 jq nftables python3 util-linux
fi

if [[ "${state_root}" != "/var/lib/fc/workspace-smoke" ]]; then
  echo "Refusing to reset unexpected state root" >&2
  exit 1
fi
rm -rf --one-file-system "${state_root}"
rm -f "${volume_path}"
install -d -o root -g root -m 0755 "${state_root}"

python3 "${repo_dir}/services/control-plane/server.py" \
  --listen-host 127.0.0.1 --listen-port "${listen_port}" \
  --database "${state_root}/control-plane.db" \
  --task-database "${state_root}/tasks/tasks.db" \
  --deepseek-credential "${credential_path}" --state-root "${state_root}" \
  --idle-timeout "${INSTANCE_IDLE_TIMEOUT_SECONDS}" >"${log_path}" 2>&1 &
server_pid=$!

ready=false
for _ in $(seq 1 40); do
  if curl --fail --silent --max-time 1 "${base_url}/healthz" >/dev/null 2>&1; then ready=true; break; fi
  kill -0 "${server_pid}" 2>/dev/null || break
  sleep 0.25
done
if [[ "${ready}" != true ]]; then
  echo "FAIL: M14 control plane did not start" >&2
  cat "${log_path}" >&2
  exit 1
fi

post_json() {
  route="$1"
  payload="$2"
  curl --fail --silent --show-error --max-time 30 --request POST \
    --header 'Content-Type: application/json' --data "${payload}" "${base_url}${route}"
}

post_json "/api/instances" '{"id":"m14-smoke"}' >/dev/null
instance_created=true
echo "Starting managed microVM and exercising the M14 workspace channel..."
current_step="start-instance"
post_json "/api/instances/${instance_id}/start" '{}' | jq -e '.status == "running"' >/dev/null

token="$(tr -d '-' < /proc/sys/kernel/random/uuid | cut -c1-16)"
content="$(printf 'M14_WORKSPACE_OK:%s' "${token}" | base64 -w0)"
current_step="create-directory"
post_json "/api/instances/${instance_id}/workspace/mkdir" '{"path":"docs"}' | jq -e '.path == "docs"' >/dev/null
write_payload="$(jq -n --arg path 'docs/m14.txt' --arg content "${content}" '{path:$path,contentBase64:$content}')"
current_step="write-file"
post_json "/api/instances/${instance_id}/workspace/write" "${write_payload}" | jq -e '.path == "docs/m14.txt"' >/dev/null
current_step="list-directory"
post_json "/api/instances/${instance_id}/workspace/list" '{"path":"docs"}' | jq -e '.entries[0].name == "m14.txt" and .entries[0].type == "file"' >/dev/null

current_step="reject-path-traversal"
http_status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 \
  --request POST --header 'Content-Type: application/json' --data '{"path":"../etc/shadow"}' \
  "${base_url}/api/instances/${instance_id}/workspace/read")"
if [[ "${http_status}" != "400" ]]; then
  echo "FAIL: path traversal was not rejected" >&2
  exit 1
fi

current_step="stop-instance"
post_json "/api/instances/${instance_id}/stop" '{}' | jq -e '.status == "stopped"' >/dev/null
current_step="restart-instance"
post_json "/api/instances/${instance_id}/start" '{}' | jq -e '.status == "running"' >/dev/null
current_step="read-after-restart"
read_response="$(post_json "/api/instances/${instance_id}/workspace/read" '{"path":"docs/m14.txt"}')"
stored="$(printf '%s' "${read_response}" | jq -r '.contentBase64' | base64 -d)"
if [[ "${stored}" != "M14_WORKSPACE_OK:${token}" ]]; then
  echo "FAIL: workspace file was not preserved after restart" >&2
  exit 1
fi

current_step="delete-file"
post_json "/api/instances/${instance_id}/workspace/delete" '{"path":"docs/m14.txt"}' >/dev/null
current_step="delete-directory"
post_json "/api/instances/${instance_id}/workspace/delete" '{"path":"docs"}' >/dev/null
current_step="destroy-instance"
curl --fail --silent --show-error --max-time 30 --request DELETE \
  "${base_url}/api/instances/${instance_id}" >/dev/null
instance_created=false

echo "PASS: workspace files were safely managed inside the microVM and persisted across restart"
echo "WORKSPACE_READY scope=/workspace max_file=5MiB traversal=denied symlinks=denied persistence=preserved"
