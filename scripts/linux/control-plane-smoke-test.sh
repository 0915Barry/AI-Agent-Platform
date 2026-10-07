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

instance_id="m10-smoke"
listen_host="127.0.0.1"
listen_port="18090"
base_url="http://${listen_host}:${listen_port}"
state_root="/var/lib/fc/control-plane-smoke"
database_path="${state_root}/control-plane.db"
task_database_path="${state_root}/tasks.db"
log_path="${state_root}/server.log"
pid_path="${state_root}/server.pid"
metadata_path="${state_root}/instances/${instance_id}.json"
volume_path="/srv/fc/volumes/instances/${instance_id}.ext4"
jail_path="/srv/jailer/firecracker/${instance_id}"
server_pid=""
instance_created=false

export DEBIAN_FRONTEND=noninteractive
missing_packages=false
for required_command in curl e2fsck ip mkfs.ext4 mount nft python3 ss truncate; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    missing_packages=true
  fi
done
if [[ "${missing_packages}" == true ]]; then
  apt-get update
  apt-get install -y --no-install-recommends curl e2fsprogs iproute2 nftables python3 util-linux
fi

if [[ ! -f "/srv/fc/artifacts/agent-rootfs.ext4" ]]; then
  echo "Runtime rootfs is missing; run build-rootfs first" >&2
  exit 1
fi
if [[ ! -x /usr/local/bin/firecracker || ! -x /usr/local/bin/jailer ]]; then
  echo "Firecracker and jailer must be installed" >&2
  exit 1
fi

cleanup() {
  if [[ "${instance_created}" == true ]] && kill -0 "${server_pid}" 2>/dev/null; then
    curl --silent --max-time 15 --request DELETE \
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
  if [[ "${volume_path}" == "/srv/fc/volumes/instances/m10-smoke.ext4" ]]; then
    rm -f "${volume_path}"
  fi
  if [[ "${jail_path}" == "/srv/jailer/firecracker/m10-smoke" ]]; then
    rm -rf --one-file-system "${jail_path}"
  fi
  rm -f "${pid_path}"
}
trap cleanup EXIT

if [[ -f "${pid_path}" ]]; then
  previous_pid="$(<"${pid_path}")"
  if [[ "${previous_pid}" =~ ^[0-9]+$ ]] && kill -0 "${previous_pid}" 2>/dev/null; then
    echo "A previous control-plane smoke server is still running with PID ${previous_pid}" >&2
    exit 1
  fi
fi

if [[ "${state_root}" != "/var/lib/fc/control-plane-smoke" ]]; then
  echo "Refusing to reset unexpected state root: ${state_root}" >&2
  exit 1
fi
if [[ -f "${metadata_path}" ]]; then
  python3 "${repo_dir}/services/instance-manager/lifecycle.py" \
    --state-root "${state_root}" destroy "${instance_id}" >/dev/null 2>&1 || true
fi
rm -rf --one-file-system "${state_root}"
rm -f "${volume_path}"
install -d -o root -g root -m 0750 "${state_root}"

python3 "${repo_dir}/services/control-plane/server.py" \
  --listen-host "${listen_host}" \
  --listen-port "${listen_port}" \
  --database "${database_path}" \
  --task-database "${task_database_path}" \
  --state-root "${state_root}" \
  --idle-timeout "${INSTANCE_IDLE_TIMEOUT_SECONDS}" \
  > "${log_path}" 2>&1 &
server_pid=$!
printf '%s\n' "${server_pid}" > "${pid_path}"

server_ready=false
for _ in $(seq 1 40); do
  if curl --fail --silent --max-time 2 "${base_url}/healthz" \
    | python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "ok"' 2>/dev/null; then
    server_ready=true
    break
  fi
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.25
done
if [[ "${server_ready}" != true ]]; then
  echo "FAIL: control plane did not start" >&2
  cat "${log_path}" >&2
  exit 1
fi
if ss -H -ltn "sport = :${listen_port}" \
  | awk '{print $4}' \
  | grep -qE '^(0\.0\.0\.0|\*):'; then
  echo "FAIL: unauthenticated M10 API is exposed on a wildcard address" >&2
  exit 1
fi

create_response="$(
  curl --fail --silent --show-error --max-time 10 \
    --request POST \
    --header 'Content-Type: application/json' \
    --data '{"id":"m10-smoke"}' \
    "${base_url}/api/instances"
)"
printf '%s' "${create_response}" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); assert r["id"] == "m10-smoke" and r["status"] == "created"'
instance_created=true
echo "CONTROL_PLANE_INSTANCE_CREATED id=${instance_id} status=created"

start_response="$(
  curl --fail --silent --show-error --max-time 90 \
    --request POST "${base_url}/api/instances/${instance_id}/start"
)"
printf '%s' "${start_response}" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); assert r["status"] == "running" and r["runtime"]["processAlive"] is True'
echo "CONTROL_PLANE_INSTANCE_STARTED id=${instance_id} status=running"

status_response="$(curl --fail --silent --show-error --max-time 10 "${base_url}/api/instances/${instance_id}")"
printf '%s' "${status_response}" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); assert r["status"] == "running" and r["runtime"]["processAlive"] is True'

heartbeat_response="$(
  curl --fail --silent --show-error --max-time 10 \
    --request POST "${base_url}/api/instances/${instance_id}/heartbeat"
)"
printf '%s' "${heartbeat_response}" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); assert r["status"] == "running"'
echo "CONTROL_PLANE_HEARTBEAT id=${instance_id} status=running"

stop_response="$(
  curl --fail --silent --show-error --max-time 20 \
    --request POST "${base_url}/api/instances/${instance_id}/stop"
)"
printf '%s' "${stop_response}" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); assert r["status"] == "stopped" and r["runtime"]["processAlive"] is False'
if [[ ! -f "${volume_path}" ]]; then
  echo "FAIL: stop removed the persistent instance volume" >&2
  exit 1
fi
echo "CONTROL_PLANE_INSTANCE_STOPPED id=${instance_id} status=stopped volume=preserved"

destroy_response="$(
  curl --fail --silent --show-error --max-time 20 \
    --request DELETE "${base_url}/api/instances/${instance_id}"
)"
printf '%s' "${destroy_response}" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); assert r == {"id":"m10-smoke","status":"destroyed"}'
instance_created=false
if [[ -e "${volume_path}" || -e "${metadata_path}" ]]; then
  echo "FAIL: destroy left instance state or volume behind" >&2
  exit 1
fi

missing_status="$(
  curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 \
    "${base_url}/api/instances/${instance_id}"
)"
if [[ "${missing_status}" != "404" ]]; then
  echo "FAIL: destroyed instance returned HTTP ${missing_status}, expected 404" >&2
  exit 1
fi

echo "PASS: control plane created, started, queried, heartbeated, stopped, and destroyed a real microVM"
echo "CONTROL_PLANE_READY bind=${listen_host}:${listen_port} storage=sqlite runtime=firecracker idle_timeout=${INSTANCE_IDLE_TIMEOUT_SECONDS}s auth=loopback-only"
