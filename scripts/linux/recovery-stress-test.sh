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
cycles="${1:-30}"
if [[ ! "${cycles}" =~ ^[1-9][0-9]*$ ]] || (( cycles > 1000 )); then
  echo "Cycle count must be an integer between 1 and 1000" >&2
  exit 1
fi
for command in curl e2fsck ip jq mount mountpoint nft pgrep python3 stat umount; do
  command -v "${command}" >/dev/null 2>&1 || {
    echo "Required command is missing: ${command}" >&2
    exit 1
  }
done
[[ -f /srv/fc/artifacts/agent-rootfs.ext4 ]] || {
  echo "Runtime rootfs is missing; run build-rootfs first" >&2
  exit 1
}
if "${repo_dir}/scripts/linux/control-plane-service.sh" status >/dev/null 2>&1; then
  echo "Stop the long-running control plane first: ./run-linux.sh stop" >&2
  exit 1
fi

instance_id="recovery-stress-instance"
state_root="/var/lib/fc/recovery-stress"
database_path="${state_root}/control-plane.db"
credential_path="${state_root}/placeholder-api-key"
log_path="${state_root}/server.log"
volume_path="/srv/fc/volumes/users/${instance_id}.ext4"
jail_path="/srv/jailer/firecracker/${instance_id}"
base_url="http://127.0.0.1:18096"
marker_relative="workspace/recovery-stress-marker.txt"
marker_token="RECOVERY_STRESS_$(tr -d '-' < /proc/sys/kernel/random/uuid)"

server_pid=""
instance_created=false
test_succeeded=false
current_cycle=0
failure_stage="initialization"
current_pid=""
current_tap=""
current_nft_family="inet"
current_nft_table=""
current_mount=""
current_sidecar_pids=()
volume_inode=""
first_slot=""
previous_generation=""

db_probe() {
  python3 - "${database_path}" "${instance_id}" "$1" <<'PY'
import json
import sqlite3
import sys

database, instance_id, action = sys.argv[1:]
with sqlite3.connect(database) as db:
    db.row_factory = sqlite3.Row
    if action == "running":
        lease = db.execute(
            "SELECT status,slot,subnet,tap,nft_table FROM network_leases WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        runtime = db.execute(
            "SELECT status,pid,process_start_ticks FROM runtime_instances WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        assert lease is not None and lease["status"] == "active", lease
        assert runtime is not None and runtime["status"] == "running", runtime
        print("|".join(str(value) for value in (*lease, *runtime)))
    elif action == "crashed":
        runtime = db.execute(
            "SELECT status,stop_reason FROM runtime_instances WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        leases = db.execute(
            "SELECT COUNT(*) FROM network_leases WHERE instance_id=?", (instance_id,)
        ).fetchone()[0]
        if runtime is None:
            print(f"missing||{leases}")
        else:
            print(f"{runtime['status']}|{runtime['stop_reason'] or ''}|{leases}")
    elif action == "snapshot":
        for table, query, parameters in (
            ("instances", "SELECT * FROM instances WHERE id=?", (instance_id,)),
            ("runtime_instances", "SELECT * FROM runtime_instances WHERE instance_id=?", (instance_id,)),
            ("runtime_sidecars", "SELECT * FROM runtime_sidecars WHERE instance_id=?", (instance_id,)),
            ("network_leases", "SELECT * FROM network_leases WHERE instance_id=?", (instance_id,)),
        ):
            rows = db.execute(query, parameters).fetchall()
            print(f"TABLE {table} rows={len(rows)}")
            for row in rows:
                print(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")))
    elif action == "destroyed":
        checks = (
            ("instances", "SELECT COUNT(*) FROM instances WHERE id=?", (instance_id,)),
            ("runtime_instances", "SELECT COUNT(*) FROM runtime_instances WHERE instance_id=?", (instance_id,)),
            ("runtime_sidecars", "SELECT COUNT(*) FROM runtime_sidecars WHERE instance_id=?", (instance_id,)),
            ("runtime_bindings", "SELECT COUNT(*) FROM runtime_bindings WHERE logical_instance_id=? OR runtime_id=?", (instance_id, instance_id)),
            ("network_leases", "SELECT COUNT(*) FROM network_leases WHERE instance_id=?", (instance_id,)),
        )
        for table, query, parameters in checks:
            count = db.execute(query, parameters).fetchone()[0]
            assert count == 0, f"{table} retained {count} rows"
PY
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

dump_diagnostics() {
  echo "RECOVERY_STRESS_DIAGNOSTICS_BEGIN cycle=${current_cycle} stage=${failure_stage}" >&2
  [[ -f "${database_path}" ]] && db_probe snapshot >&2 || true
  echo "PROCESS_SNAPSHOT" >&2
  pgrep -af "${instance_id}" >&2 || true
  echo "NETWORK_SNAPSHOT" >&2
  ip tuntap show 2>/dev/null | grep '^fc' >&2 || true
  ip -4 route show 2>/dev/null | grep '172\.29\.' >&2 || true
  nft list tables 2>/dev/null | grep 'aap_' >&2 || true
  stat "${volume_path}" >&2 || true
  for log in "${log_path}" \
    "${state_root}/control-plane/${instance_id}/console.log" \
    "${state_root}/control-plane/${instance_id}/reaper.log" \
    "${state_root}/control-plane/${instance_id}/sidecars/gateway.log"; do
    if [[ -f "${log}" ]]; then
      echo "LOG_TAIL_BEGIN path=${log}" >&2
      tail -n 120 "${log}" >&2
      echo "LOG_TAIL_END path=${log}" >&2
    fi
  done
  echo "RECOVERY_STRESS_DIAGNOSTICS_END" >&2
}

cleanup() {
  set +e
  if [[ -n "${current_mount}" ]] && mountpoint -q "${current_mount}"; then
    umount "${current_mount}" >/dev/null 2>&1 || true
  fi
  [[ -n "${current_mount}" ]] && rmdir "${current_mount}" >/dev/null 2>&1 || true
  if [[ "${instance_created}" == true && -n "${server_pid}" ]] \
    && kill -0 "${server_pid}" 2>/dev/null; then
    curl --silent --max-time 20 --request DELETE \
      "${base_url}/api/instances/${instance_id}" >/dev/null 2>&1 || true
  fi
  terminate_pid "${current_pid}"
  for pid in "${current_sidecar_pids[@]}"; do terminate_pid "${pid}"; done
  [[ -n "${current_nft_table}" ]] \
    && nft delete table "${current_nft_family}" "${current_nft_table}" >/dev/null 2>&1 || true
  [[ -n "${current_tap}" ]] && ip link delete "${current_tap}" >/dev/null 2>&1 || true
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
  fi
  [[ -f "${database_path}" ]] && python3 "${repo_dir}/services/instance-manager/lifecycle.py" \
    --state-root "${state_root}" --database "${database_path}" \
    destroy "${instance_id}" >/dev/null 2>&1 || true
  rm -rf --one-file-system "${jail_path}"
  rm -f "${volume_path}"
  [[ "${test_succeeded}" == true ]] && rm -rf --one-file-system "${state_root}"
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

[[ "${state_root}" == "/var/lib/fc/recovery-stress" ]] || exit 1
rm -rf --one-file-system "${state_root}"
rm -f "${volume_path}"
install -d -o root -g root -m 0755 "${state_root}"
printf '%s\n' 'recovery-stress-placeholder' > "${credential_path}"
chmod 0600 "${credential_path}"

failure_stage="control_plane_start"
python3 "${repo_dir}/services/control-plane/server.py" \
  --listen-host 127.0.0.1 --listen-port 18096 \
  --database "${database_path}" --deepseek-credential "${credential_path}" \
  --state-root "${state_root}" --idle-timeout "${INSTANCE_IDLE_TIMEOUT_SECONDS}" \
  > "${log_path}" 2>&1 &
server_pid=$!
ready=false
for _ in $(seq 1 40); do
  if curl --fail --silent --max-time 1 "${base_url}/healthz" >/dev/null 2>&1; then
    ready=true; break
  fi
  kill -0 "${server_pid}" 2>/dev/null || break
  sleep 0.25
done
[[ "${ready}" == true ]]

failure_stage="instance_create"
curl --fail --silent --show-error --max-time 10 --request POST \
  --header 'Content-Type: application/json' --data "{\"id\":\"${instance_id}\"}" \
  "${base_url}/api/instances" >/dev/null
instance_created=true

# 正常启停一次以创建数据盘，然后离线写入跨崩溃周期不变的内容探针。
failure_stage="marker_volume_prepare"
curl --fail --silent --show-error --max-time 90 --request POST \
  "${base_url}/api/instances/${instance_id}/start" \
  | jq -e '.status == "running" and .runtime.processAlive == true' >/dev/null
curl --fail --silent --show-error --max-time 30 --request POST \
  "${base_url}/api/instances/${instance_id}/stop" \
  | jq -e '.status == "stopped"' >/dev/null
volume_inode="$(stat -c '%i' "${volume_path}")"
current_mount="$(mktemp -d /var/tmp/recovery-stress-seed.XXXXXX)"
mount -o loop "${volume_path}" "${current_mount}"
printf '%s\n' "${marker_token}" > "${current_mount}/${marker_relative}"
chown 1001:1001 "${current_mount}/${marker_relative}"
chmod 0600 "${current_mount}/${marker_relative}"
sync
umount "${current_mount}"; rmdir "${current_mount}"; current_mount=""

for current_cycle in $(seq 1 "${cycles}"); do
  echo "===== Recovery ${current_cycle}/${cycles} ====="
  current_pid=""; current_tap=""; current_nft_table=""; current_sidecar_pids=()

  failure_stage="start_request"
  response_path="${state_root}/start-response-${current_cycle}.json"
  started_ms="$(date +%s%3N)"
  status="$(curl --silent --show-error --max-time 90 --output "${response_path}" \
    --write-out '%{http_code}' --request POST "${base_url}/api/instances/${instance_id}/start")"
  [[ "${status}" == 200 ]] || { cat "${response_path}" >&2; false; }
  jq -e '.status == "running" and .runtime.processAlive == true' "${response_path}" >/dev/null
  start_ms="$(( $(date +%s%3N) - started_ms ))"
  current_pid="$(jq -r '.runtime.pid' "${response_path}")"
  start_ticks="$(jq -r '.runtime.processStartTicks' "${response_path}")"
  current_tap="$(jq -r '.runtime.tapName' "${response_path}")"
  current_nft_family="$(jq -r '.runtime.nftFamily' "${response_path}")"
  current_nft_table="$(jq -r '.runtime.nftTable' "${response_path}")"
  subnet="$(jq -r '.runtime.networkSubnet' "${response_path}")"
  mapfile -t current_sidecar_pids < <(jq -r '.runtime.sidecars[]?.pid' "${response_path}")

  failure_stage="running_resource_probe"
  generation="${current_pid}:${start_ticks}"
  [[ "${generation}" != "${previous_generation}" ]]
  kill -0 "${current_pid}"
  [[ "$(process_start_ticks "${current_pid}")" == "${start_ticks}" ]]
  for pid in "${current_sidecar_pids[@]}"; do kill -0 "${pid}"; done
  ip link show dev "${current_tap}" >/dev/null
  nft list table "${current_nft_family}" "${current_nft_table}" >/dev/null
  [[ -S "${jail_path}/root/firecracker.socket" ]]
  [[ "$(stat -c '%i' "${volume_path}")" == "${volume_inode}" ]]
  running_probe="$(db_probe running)"
  IFS='|' read -r _ slot _ _ _ _ db_pid db_ticks <<< "${running_probe}"
  [[ "${db_pid}:${db_ticks}" == "${generation}" ]]
  if [[ -z "${first_slot}" ]]; then first_slot="${slot}"; else [[ "${slot}" == "${first_slot}" ]]; fi

  failure_stage="firecracker_crash"
  crash_ms="$(date +%s%3N)"
  kill -KILL "${current_pid}"

  failure_stage="watcher_reconciliation"
  reconciled=false
  for _ in $(seq 1 120); do
    state="$(db_probe crashed)"
    if [[ "${state}" == "stopped|process_exited|0" && ! -e "${jail_path}" ]]; then
      reconciled=true; break
    fi
    sleep 0.1
  done
  [[ "${reconciled}" == true ]]
  reconcile_ms="$(( $(date +%s%3N) - crash_ms ))"

  failure_stage="crash_cleanup_probe"
  if kill -0 "${current_pid}" 2>/dev/null; then false; fi
  for pid in "${current_sidecar_pids[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then false; fi
  done
  for _ in $(seq 1 20); do
    pgrep -f "lifecycle.py.*watch.*${instance_id}" >/dev/null 2>&1 || break
    sleep 0.1
  done
  if pgrep -f "lifecycle.py.*watch.*${instance_id}" >/dev/null 2>&1; then false; fi
  if ip link show dev "${current_tap}" >/dev/null 2>&1; then false; fi
  if nft list table "${current_nft_family}" "${current_nft_table}" >/dev/null 2>&1; then false; fi
  [[ ! -e "${state_root}/stop-intents/${instance_id}" ]]
  [[ "$(stat -c '%i' "${volume_path}")" == "${volume_inode}" ]]

  failure_stage="data_volume_probe"
  fsck_started_ms="$(date +%s%3N)"; fsck_status=0
  e2fsck -f -y "${volume_path}" > "${state_root}/e2fsck-${current_cycle}.log" 2>&1 \
    || fsck_status="$?"
  (( fsck_status <= 1 ))
  fsck_ms="$(( $(date +%s%3N) - fsck_started_ms ))"
  current_mount="$(mktemp -d /var/tmp/recovery-stress-check.XXXXXX)"
  mount -o loop,ro "${volume_path}" "${current_mount}"
  [[ "$(<"${current_mount}/${marker_relative}")" == "${marker_token}" ]]
  umount "${current_mount}"; rmdir "${current_mount}"; current_mount=""

  failure_stage="api_reconciliation_probe"
  api_response_path="${state_root}/reconciled-response-${current_cycle}.json"
  curl --fail --silent --show-error --max-time 10 \
    --output "${api_response_path}" "${base_url}/api/instances/${instance_id}"
  # stopped 业务记录不会附加 runtime 对象；进程消失和运行时 stopped 已由
  # 前面的 PID 与 SQLite 探针独立验证，这里只校验 Control Plane 对外状态。
  if ! jq -e '.status == "stopped"' "${api_response_path}" >/dev/null; then
    echo "Unexpected reconciled API response:" >&2
    cat "${api_response_path}" >&2
    false
  fi

  echo "RECOVERY_STRESS_ROUND cycle=${current_cycle} status=pass start_ms=${start_ms} reconcile_ms=${reconcile_ms} fsck_ms=${fsck_ms} pid=${current_pid} start_ticks=${start_ticks} slot=${slot} subnet=${subnet} tap=${current_tap} data=preserved"
  previous_generation="${generation}"
done

failure_stage="destroy_request"
curl --fail --silent --show-error --max-time 30 --request DELETE \
  "${base_url}/api/instances/${instance_id}" >/dev/null
instance_created=false
failure_stage="final_probe"
db_probe destroyed
[[ ! -e "${jail_path}" && ! -e "${volume_path}" ]]
if pgrep -f "${instance_id}" >/dev/null 2>&1; then false; fi
if ip link show dev "${current_tap}" >/dev/null 2>&1; then false; fi
if nft list table "${current_nft_family}" "${current_nft_table}" >/dev/null 2>&1; then false; fi

test_succeeded=true
echo "PASS: recovery stress test completed ${cycles} Firecracker crash cycles"
echo "RECOVERY_STRESS_READY cycles=${cycles} process_generation=verified crash_reaping=verified state=reconciled network=clean data=preserved filesystem=clean restart=verified volume=destroyed"
