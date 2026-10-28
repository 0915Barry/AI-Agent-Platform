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

instance_id="network-lease-smoke"
listen_port="18092"
base_url="http://127.0.0.1:${listen_port}"
state_root="/var/lib/fc/network-lease-smoke"
database_path="${state_root}/control-plane.db"
task_database_path="${state_root}/tasks/tasks.db"
credential_path="${state_root}/placeholder-api-key"
volume_path="/srv/fc/volumes/instances/${instance_id}.ext4"
jail_path="/srv/jailer/firecracker/${instance_id}"
log_path="${state_root}/server.log"
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
  python3 "${repo_dir}/services/instance-manager/lifecycle.py" \
    --state-root "${state_root}" --database "${database_path}" \
    destroy "${instance_id}" >/dev/null 2>&1 || true
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
if [[ "${state_root}" != "/var/lib/fc/network-lease-smoke" ]]; then
  echo "Refusing to reset unexpected state root: ${state_root}" >&2
  exit 1
fi
python3 "${repo_dir}/services/instance-manager/lifecycle.py" \
  --state-root "${state_root}" --database "${database_path}" \
  destroy "${instance_id}" >/dev/null 2>&1 || true
rm -rf --one-file-system "${state_root}"
rm -f "${volume_path}"
# Sidecar 以 agent-gateway 低权限用户运行，需要能够穿过状态根目录；真正的凭据
# 仍位于下层 0750 目录并保持 0400，因此这里只开放目录遍历而不开放文件读取。
install -d -o root -g root -m 0755 "${state_root}"
printf '%s\n' 'network-smoke-placeholder' > "${credential_path}"
chmod 0600 "${credential_path}"

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

server_ready=false
for _ in $(seq 1 40); do
  if curl --fail --silent --max-time 1 "${base_url}/healthz" >/dev/null 2>&1; then
    server_ready=true
    break
  fi
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.25
done
if [[ "${server_ready}" != true ]]; then
  echo "FAIL: network lease smoke control plane did not start" >&2
  cat "${log_path}" >&2
  exit 1
fi

curl --fail --silent --show-error --max-time 10 \
  --request POST --header 'Content-Type: application/json' \
  --data '{"id":"network-lease-smoke"}' \
  "${base_url}/api/instances" >/dev/null
instance_created=true

echo "Starting one managed microVM without making a model request..."
start_response_path="${state_root}/start-response.json"
start_http_status="$(
  curl --silent --show-error --max-time 120 \
    --output "${start_response_path}" \
    --write-out '%{http_code}' \
    --request POST "${base_url}/api/instances/${instance_id}/start"
)"
start_response="$(<"${start_response_path}")"
if [[ "${start_http_status}" != "200" ]]; then
  echo "FAIL: managed microVM start returned HTTP ${start_http_status}" >&2
  echo "--- API response ---" >&2
  printf '%s\n' "${start_response}" >&2
  echo "--- control-plane log ---" >&2
  tail -n 120 "${log_path}" >&2
  exit 1
fi
printf '%s' "${start_response}" \
  | jq -e '.status == "running" and .runtime.processAlive == true and .runtime.rpcPort == 8080' >/dev/null

tap_name="$(printf '%s' "${start_response}" | jq -r '.runtime.tapName')"
nft_family="$(printf '%s' "${start_response}" | jq -r '.runtime.nftFamily')"
nft_table="$(printf '%s' "${start_response}" | jq -r '.runtime.nftTable')"
subnet="$(printf '%s' "${start_response}" | jq -r '.runtime.networkSubnet')"
host_ip="$(printf '%s' "${start_response}" | jq -r '.runtime.hostIp')"
guest_ip="$(printf '%s' "${start_response}" | jq -r '.runtime.guestIp')"
rpc_port="$(printf '%s' "${start_response}" | jq -r '.runtime.rpcPort')"

# 直接连接正式受管实例的 Pi RPC 端口，验证不再依赖实验实例或 Agent Bridge。
python3 - "${guest_ip}" "${rpc_port}" <<'PY'
import json
import socket
import sys

with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5) as connection:
    connection.settimeout(10)
    connection.sendall(b'{"id":"managed-rpc-smoke","type":"get_state"}\n')
    buffer = b""
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            raise SystemExit("Pi RPC connection closed before get_state response")
        buffer += chunk
        while b"\n" in buffer:
            raw_line, buffer = buffer.split(b"\n", 1)
            if not raw_line:
                continue
            event = json.loads(raw_line)
            if event.get("id") == "managed-rpc-smoke" and event.get("type") == "response":
                assert event.get("success") is True, event
                assert event.get("command") == "get_state", event
                raise SystemExit(0)
PY

python3 - "${database_path}" "${instance_id}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    lease = connection.execute(
        "SELECT status,subnet FROM network_leases WHERE instance_id=?", (sys.argv[2],)
    ).fetchone()
assert lease is not None, "network lease was not stored in SQLite"
assert lease[0] == "active", lease
assert lease[1].endswith("/30"), lease
PY
ip link show dev "${tap_name}" >/dev/null
ip -4 address show dev "${tap_name}" | grep -Fq "${host_ip}/30"
nft list table "${nft_family}" "${nft_table}" >/dev/null

curl --fail --silent --show-error --max-time 30 \
  --request POST "${base_url}/api/instances/${instance_id}/stop" \
  | jq -e '.status == "stopped" and .runtime.processAlive == false' >/dev/null

python3 - "${database_path}" "${instance_id}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    count = connection.execute(
        "SELECT COUNT(*) FROM network_leases WHERE instance_id=?", (sys.argv[2],)
    ).fetchone()[0]
assert count == 0, "network lease remained in SQLite after stop"
PY
if ip link show dev "${tap_name}" >/dev/null 2>&1; then
  echo "FAIL: TAP remained after instance stop: ${tap_name}" >&2
  exit 1
fi
if nft list table "${nft_family}" "${nft_table}" >/dev/null 2>&1; then
  echo "FAIL: nftables table remained after instance stop: ${nft_table}" >&2
  exit 1
fi
if [[ ! -f "${volume_path}" ]]; then
  echo "FAIL: stopping the instance removed its persistent data volume" >&2
  exit 1
fi

curl --fail --silent --show-error --max-time 30 \
  --request DELETE "${base_url}/api/instances/${instance_id}" >/dev/null
instance_created=false

python3 - "${database_path}" "${instance_id}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    count = connection.execute(
        "SELECT COUNT(*) FROM runtime_instances WHERE instance_id=?", (sys.argv[2],)
    ).fetchone()[0]
assert count == 0, "runtime metadata remained after destroy"
PY

echo "PASS: managed instance Pi RPC, network allocation, and cleanup completed without a model request"
echo "MANAGED_RPC_READY guest=${guest_ip}:${rpc_port} transport=tcp protocol=jsonl bridge=absent mcp=deferred"
echo "NETWORK_LEASE_READY pool=172.29.0.0/16 subnet=${subnet} host=${host_ip} guest=${guest_ip} tap=${tap_name} state=released"
