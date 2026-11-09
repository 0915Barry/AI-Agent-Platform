#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"

# shellcheck disable=SC1091
. "${repo_dir}/config/lifecycle.env"
# shellcheck disable=SC1091
. "${repo_dir}/scripts/linux/proc-stat.sh"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi

cycles="${1:-100}"
if [[ ! "${cycles}" =~ ^[1-9][0-9]*$ ]] || (( cycles > 1000 )); then
  echo "Cycle count must be an integer between 1 and 1000" >&2
  exit 1
fi

for required_command in curl ip jq nft pgrep python3; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "Required command is missing: ${required_command}" >&2
    exit 1
  fi
done
if [[ ! -f /srv/fc/artifacts/agent-rootfs.ext4 ]]; then
  echo "Runtime rootfs is missing; run build-rootfs first" >&2
  exit 1
fi
if "${repo_dir}/scripts/linux/control-plane-service.sh" status >/dev/null 2>&1; then
  echo "Stop the long-running control plane before lifecycle-stress-test: ./run-linux.sh stop" >&2
  exit 1
fi

instance_id="lifecycle-stress-instance"
listen_port="18095"
base_url="http://127.0.0.1:${listen_port}"
state_root="/var/lib/fc/lifecycle-stress"
database_path="${state_root}/control-plane.db"
credential_path="${state_root}/placeholder-api-key"
volume_path="/srv/fc/volumes/users/${instance_id}.ext4"
jail_path="/srv/jailer/firecracker/${instance_id}"
log_path="${state_root}/server.log"
server_pid=""
instance_created=false
test_succeeded=false
current_cycle=0
failure_stage="initialization"
current_pid=""
current_start_ticks=""
current_tap=""
current_nft_family="inet"
current_nft_table=""
current_subnet=""
current_sidecar_pids=()
first_slot=""
previous_pid=""
previous_start_ticks=""

dump_diagnostics() {
  echo "LIFECYCLE_STRESS_DIAGNOSTICS_BEGIN cycle=${current_cycle} stage=${failure_stage}" >&2

  if [[ -f "${database_path}" ]]; then
    python3 - "${database_path}" "${instance_id}" <<'PY' >&2 || true
import json
import sqlite3
import sys

database, instance_id = sys.argv[1:]
tables = {
    "instances": ("SELECT * FROM instances WHERE id=?", (instance_id,)),
    "runtime_instances": (
        "SELECT * FROM runtime_instances WHERE instance_id=?", (instance_id,)
    ),
    "runtime_sidecars": (
        "SELECT * FROM runtime_sidecars WHERE instance_id=?", (instance_id,)
    ),
    "runtime_bindings": (
        "SELECT * FROM runtime_bindings WHERE logical_instance_id=? OR runtime_id=?",
        (instance_id, instance_id),
    ),
    "network_leases": (
        "SELECT * FROM network_leases WHERE instance_id=?", (instance_id,)
    ),
}
with sqlite3.connect(database) as connection:
    connection.row_factory = sqlite3.Row
    existing = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table, (query, parameters) in tables.items():
        if table not in existing:
            print(f"TABLE {table} missing")
            continue
        rows = connection.execute(query, parameters).fetchall()
        print(f"TABLE {table} rows={len(rows)}")
        for row in rows:
            print(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")))
PY
  else
    echo "DATABASE_MISSING path=${database_path}" >&2
  fi

  echo "PROCESS_SNAPSHOT" >&2
  pgrep -af "${instance_id}" >&2 || true
  echo "NETWORK_SNAPSHOT" >&2
  ip tuntap show 2>/dev/null | grep '^fc' >&2 || true
  ip -4 route show 2>/dev/null | grep '172\.29\.' >&2 || true
  nft list tables 2>/dev/null | grep 'aap_' >&2 || true
  echo "FILESYSTEM_SNAPSHOT" >&2
  find "${jail_path}" -maxdepth 2 -mindepth 0 -print 2>/dev/null | head -n 80 >&2 || true

  for candidate in \
    "${log_path}" \
    "${state_root}/control-plane/${instance_id}/console.log" \
    "${state_root}/control-plane/${instance_id}/reaper.log" \
    "${state_root}/control-plane/${instance_id}/sidecars/gateway.log"; do
    if [[ -f "${candidate}" ]]; then
      echo "LOG_TAIL_BEGIN path=${candidate}" >&2
      tail -n 120 "${candidate}" >&2
      echo "LOG_TAIL_END path=${candidate}" >&2
    fi
  done
  echo "LIFECYCLE_STRESS_DIAGNOSTICS_END" >&2
}

terminate_pid() {
  local pid="$1"
  if [[ "${pid}" =~ ^[1-9][0-9]*$ ]] && kill -0 "${pid}" 2>/dev/null; then
    kill -TERM "${pid}" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "${pid}" 2>/dev/null || return 0
      sleep 0.1
    done
    kill -KILL "${pid}" 2>/dev/null || true
  fi
}

cleanup() {
  set +e
  if [[ "${instance_created}" == true && -n "${server_pid}" ]] \
    && kill -0 "${server_pid}" 2>/dev/null; then
    curl --silent --max-time 20 --request DELETE \
      "${base_url}/api/instances/${instance_id}" >/dev/null 2>&1 || true
  fi

  terminate_pid "${current_pid}"
  for pid in "${current_sidecar_pids[@]}"; do
    terminate_pid "${pid}"
  done

  if [[ -n "${current_nft_table}" ]]; then
    nft delete table "${current_nft_family}" "${current_nft_table}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${current_tap}" ]]; then
    ip link delete "${current_tap}" >/dev/null 2>&1 || true
  fi

  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
  fi

  if [[ -f "${database_path}" ]]; then
    python3 "${repo_dir}/services/instance-manager/lifecycle.py" \
      --state-root "${state_root}" --database "${database_path}" \
      destroy "${instance_id}" >/dev/null 2>&1 || true
  fi

  terminate_pid "${current_pid}"
  for pid in "${current_sidecar_pids[@]}"; do
    terminate_pid "${pid}"
  done
  rm -rf --one-file-system "${jail_path}"
  rm -f "${volume_path}"

  if [[ "${test_succeeded}" == true ]]; then
    rm -rf --one-file-system "${state_root}"
  fi
}

on_error() {
  local status="$?"
  trap - ERR
  echo "FAIL_STAGE=${failure_stage} cycle=${current_cycle} exit_status=${status}" >&2
  dump_diagnostics
  exit "${status}"
}

trap on_error ERR
trap cleanup EXIT

if [[ "${state_root}" != "/var/lib/fc/lifecycle-stress" ]]; then
  echo "Refusing to reset unexpected state root: ${state_root}" >&2
  exit 1
fi
rm -rf --one-file-system "${state_root}"
rm -f "${volume_path}"
install -d -o root -g root -m 0755 "${state_root}"
printf '%s\n' 'lifecycle-stress-placeholder' > "${credential_path}"
chmod 0600 "${credential_path}"

failure_stage="control_plane_start"
python3 "${repo_dir}/services/control-plane/server.py" \
  --listen-host 127.0.0.1 \
  --listen-port "${listen_port}" \
  --database "${database_path}" \
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
  kill -0 "${server_pid}" 2>/dev/null || break
  sleep 0.25
done
if [[ "${server_ready}" != true ]]; then
  echo "FAIL_STAGE=control_plane_start reason=health_check_failed" >&2
  dump_diagnostics
  exit 1
fi

failure_stage="instance_create"
curl --fail --silent --show-error --max-time 10 \
  --request POST --header 'Content-Type: application/json' \
  --data "{\"id\":\"${instance_id}\"}" \
  "${base_url}/api/instances" >/dev/null
instance_created=true

for current_cycle in $(seq 1 "${cycles}"); do
  echo "===== Lifecycle ${current_cycle}/${cycles} ====="
  current_pid=""
  current_start_ticks=""
  current_tap=""
  current_nft_table=""
  current_subnet=""
  current_sidecar_pids=()

  failure_stage="start_request"
  start_response_path="${state_root}/start-response-${current_cycle}.json"
  started_ms="$(date +%s%3N)"
  start_http_status="$(
    curl --silent --show-error --max-time 90 \
      --output "${start_response_path}" --write-out '%{http_code}' \
      --request POST "${base_url}/api/instances/${instance_id}/start"
  )"
  if [[ "${start_http_status}" != "200" ]]; then
    echo "START_RESPONSE status=${start_http_status}" >&2
    cat "${start_response_path}" >&2
    false
  fi
  start_response="$(<"${start_response_path}")"
  printf '%s' "${start_response}" \
    | jq -e '.status == "running" and .runtime.processAlive == true' >/dev/null
  start_ms="$(( $(date +%s%3N) - started_ms ))"

  current_pid="$(printf '%s' "${start_response}" | jq -r '.runtime.pid')"
  current_start_ticks="$(printf '%s' "${start_response}" | jq -r '.runtime.processStartTicks')"
  current_tap="$(printf '%s' "${start_response}" | jq -r '.runtime.tapName')"
  current_nft_family="$(printf '%s' "${start_response}" | jq -r '.runtime.nftFamily')"
  current_nft_table="$(printf '%s' "${start_response}" | jq -r '.runtime.nftTable')"
  current_subnet="$(printf '%s' "${start_response}" | jq -r '.runtime.networkSubnet')"
  mapfile -t current_sidecar_pids < <(
    printf '%s' "${start_response}" | jq -r '.runtime.sidecars[]?.pid'
  )

  failure_stage="running_process_probe"
  kill -0 "${current_pid}"
  observed_ticks="$(process_start_ticks "${current_pid}")"
  [[ "${observed_ticks}" == "${current_start_ticks}" ]]
  if [[ -n "${previous_pid}" && "${current_pid}:${current_start_ticks}" == "${previous_pid}:${previous_start_ticks}" ]]; then
    echo "Process generation did not change across restart" >&2
    false
  fi
  for pid in "${current_sidecar_pids[@]}"; do
    kill -0 "${pid}"
  done

  failure_stage="running_database_probe"
  lease_probe="$(python3 - "${database_path}" "${instance_id}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    lease = connection.execute(
        "SELECT status,slot,subnet,tap,nft_table FROM network_leases WHERE instance_id=?",
        (sys.argv[2],),
    ).fetchone()
    runtime = connection.execute(
        "SELECT status,pid,process_start_ticks FROM runtime_instances WHERE instance_id=?",
        (sys.argv[2],),
    ).fetchone()
assert lease is not None, "active network lease is missing"
assert runtime is not None, "runtime metadata is missing"
assert lease[0] == "active", lease
assert runtime[0] == "running", runtime
print("|".join(str(value) for value in (*lease, *runtime)))
PY
)"
  IFS='|' read -r lease_status lease_slot lease_subnet lease_tap lease_nft \
    runtime_status runtime_pid runtime_ticks <<< "${lease_probe}"
  [[ "${runtime_pid}" == "${current_pid}" ]]
  [[ "${runtime_ticks}" == "${current_start_ticks}" ]]
  [[ "${lease_tap}" == "${current_tap}" ]]
  [[ "${lease_nft}" == "${current_nft_table}" ]]
  if [[ -z "${first_slot}" ]]; then
    first_slot="${lease_slot}"
  else
    [[ "${lease_slot}" == "${first_slot}" ]]
  fi

  failure_stage="running_linux_resource_probe"
  ip link show dev "${current_tap}" >/dev/null
  nft list table "${current_nft_family}" "${current_nft_table}" >/dev/null
  [[ -d "${jail_path}" ]]
  [[ -S "${jail_path}/root/firecracker.socket" ]]
  [[ -f "${volume_path}" ]]

  failure_stage="stop_request"
  stopped_ms="$(date +%s%3N)"
  stop_response_path="${state_root}/stop-response-${current_cycle}.json"
  stop_http_status="$(
    curl --silent --show-error --max-time 30 \
      --output "${stop_response_path}" --write-out '%{http_code}' \
      --request POST "${base_url}/api/instances/${instance_id}/stop"
  )"
  if [[ "${stop_http_status}" != "200" ]]; then
    echo "STOP_RESPONSE status=${stop_http_status}" >&2
    cat "${stop_response_path}" >&2
    false
  fi
  jq -e '.status == "stopped" and .runtime.processAlive == false' \
    < "${stop_response_path}" >/dev/null
  stop_ms="$(( $(date +%s%3N) - stopped_ms ))"

  failure_stage="stopped_process_probe"
  for _ in $(seq 1 30); do
    kill -0 "${current_pid}" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 "${current_pid}" 2>/dev/null; then
    echo "Firecracker process remained after stop: ${current_pid}" >&2
    false
  fi
  for pid in "${current_sidecar_pids[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      echo "Sidecar process remained after stop: ${pid}" >&2
      false
    fi
  done
  for _ in $(seq 1 20); do
    if ! pgrep -f "lifecycle.py.*watch.*${instance_id}" >/dev/null 2>&1; then
      break
    fi
    sleep 0.1
  done
  if pgrep -f "lifecycle.py.*watch.*${instance_id}" >/dev/null 2>&1; then
    echo "Lifecycle watcher remained after stop" >&2
    false
  fi

  failure_stage="stopped_database_probe"
  python3 - "${database_path}" "${instance_id}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    lease_count = connection.execute(
        "SELECT COUNT(*) FROM network_leases WHERE instance_id=?", (sys.argv[2],)
    ).fetchone()[0]
    runtime = connection.execute(
        "SELECT status FROM runtime_instances WHERE instance_id=?", (sys.argv[2],)
    ).fetchone()
assert lease_count == 0, "network lease remained after stop"
assert runtime is not None and runtime[0] == "stopped", runtime
PY

  failure_stage="stopped_linux_resource_probe"
  if ip link show dev "${current_tap}" >/dev/null 2>&1; then
    echo "TAP remained after stop: ${current_tap}" >&2
    false
  fi
  if nft list table "${current_nft_family}" "${current_nft_table}" >/dev/null 2>&1; then
    echo "nftables table remained after stop: ${current_nft_table}" >&2
    false
  fi
  [[ ! -e "${jail_path}" ]]
  [[ ! -e "${state_root}/stop-intents/${instance_id}" ]]
  [[ -f "${volume_path}" ]]

  echo "LIFECYCLE_STRESS_ROUND cycle=${current_cycle} status=pass start_ms=${start_ms} stop_ms=${stop_ms} pid=${current_pid} start_ticks=${current_start_ticks} slot=${lease_slot} subnet=${current_subnet} tap=${current_tap}"
  previous_pid="${current_pid}"
  previous_start_ticks="${current_start_ticks}"
done

failure_stage="destroy_request"
curl --fail --silent --show-error --max-time 30 \
  --request DELETE "${base_url}/api/instances/${instance_id}" >/dev/null
instance_created=false

failure_stage="final_database_probe"
python3 - "${database_path}" "${instance_id}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    checks = (
        ("instances", "SELECT COUNT(*) FROM instances WHERE id=?", (sys.argv[2],)),
        (
            "runtime_instances",
            "SELECT COUNT(*) FROM runtime_instances WHERE instance_id=?",
            (sys.argv[2],),
        ),
        (
            "runtime_sidecars",
            "SELECT COUNT(*) FROM runtime_sidecars WHERE instance_id=?",
            (sys.argv[2],),
        ),
        (
            "runtime_bindings",
            "SELECT COUNT(*) FROM runtime_bindings "
            "WHERE logical_instance_id=? OR runtime_id=?",
            (sys.argv[2], sys.argv[2]),
        ),
        (
            "network_leases",
            "SELECT COUNT(*) FROM network_leases WHERE instance_id=?",
            (sys.argv[2],),
        ),
    )
    for table, query, parameters in checks:
        count = connection.execute(query, parameters).fetchone()[0]
        assert count == 0, f"{table} retained {count} rows"
PY

failure_stage="final_linux_resource_probe"
[[ ! -e "${jail_path}" ]]
[[ ! -e "${volume_path}" ]]
if pgrep -f "${instance_id}" >/dev/null 2>&1; then
  echo "Instance process remained after destroy" >&2
  false
fi
if ip link show dev "${current_tap}" >/dev/null 2>&1; then
  echo "TAP remained after destroy: ${current_tap}" >&2
  false
fi
if nft list table "${current_nft_family}" "${current_nft_table}" >/dev/null 2>&1; then
  echo "nftables table remained after destroy: ${current_nft_table}" >&2
  false
fi

test_succeeded=true
echo "PASS: lifecycle stress test completed ${cycles} start/stop cycles without leaked resources"
echo "LIFECYCLE_STRESS_READY cycles=${cycles} slot=${first_slot} process_generation=verified lease=verified tap=clean nft=clean watcher=clean volume=destroyed"
