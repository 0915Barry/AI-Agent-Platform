#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"

# shellcheck disable=SC1091
. "${repo_dir}/config/versions.env"

source_id="snapshot-smoke-source"
restore_id="snapshot-smoke-restored"
jailer_base="/srv/jailer"
source_jail="${jailer_base}/firecracker/${source_id}"
restore_jail="${jailer_base}/firecracker/${restore_id}"
source_root="${source_jail}/root"
restore_root="${restore_jail}/root"
state_root="/var/lib/fc/snapshot-smoke"
snapshot_root="/srv/fc/snapshots/smoke"
volume_path="/srv/fc/volumes/smoke/snapshot.ext4"
kernel_path="/srv/fc/artifacts/vmlinux-${SMOKE_KERNEL_VERSION}"
rootfs_path="/srv/fc/artifacts/agent-rootfs.ext4"
test_rootfs="${state_root}/rootfs.ext4"
source_console="${state_root}/source-console.log"
restore_console="${state_root}/restore-console.log"
tap_name="fcsnapshot"
nft_family="inet"
nft_table="aap_snapshot"
host_ip="172.31.249.1"
host_cidr="${host_ip}/30"
guest_ip="172.31.249.2"
guest_cidr="${guest_ip}/30"
guest_mac="06:00:ac:1f:f9:02"
rpc_port="8080"
marker="SNAPSHOT_DATA_$RANDOM$RANDOM"
current_pid=""

for required in \
  /usr/local/bin/firecracker \
  /usr/local/bin/jailer \
  "${kernel_path}" \
  "${rootfs_path}"; do
  if [[ ! -e "${required}" ]]; then
    echo "Required snapshot test asset is missing: ${required}" >&2
    exit 1
  fi
done

firecracker_uid="$(id -u firecracker)"
firecracker_gid="$(id -g firecracker)"

stop_current_vm() {
  if [[ -n "${current_pid}" ]] && kill -0 "${current_pid}" 2>/dev/null; then
    kill "${current_pid}" 2>/dev/null || true
    for _ in $(seq 1 20); do
      if ! kill -0 "${current_pid}" 2>/dev/null; then
        break
      fi
      sleep 0.1
    done
    if kill -0 "${current_pid}" 2>/dev/null; then
      kill -9 "${current_pid}" 2>/dev/null || true
    fi
    wait "${current_pid}" 2>/dev/null || true
  fi
  current_pid=""
}

cleanup_network() {
  if nft list table "${nft_family}" "${nft_table}" >/dev/null 2>&1; then
    nft delete table "${nft_family}" "${nft_table}" >/dev/null 2>&1 || true
  fi
  if ip link show dev "${tap_name}" >/dev/null 2>&1; then
    ip link delete "${tap_name}" >/dev/null 2>&1 || true
  fi
}

cleanup() {
  stop_current_vm
  cleanup_network
  rm -rf --one-file-system "${source_jail}" "${restore_jail}"
  rm -rf --one-file-system "${state_root}" "${snapshot_root}"
  rm -f "${volume_path}"
}

report_unexpected_failure() {
  local status="$?"
  trap - ERR
  echo "FAIL: snapshot smoke test stopped unexpectedly (status ${status})" >&2
  if [[ -s "${source_console}" ]]; then
    echo "--- source guest console tail ---" >&2
    tail -n 120 "${source_console}" >&2 || true
  fi
  if [[ -s "${restore_console}" ]]; then
    echo "--- restored guest console tail ---" >&2
    tail -n 120 "${restore_console}" >&2 || true
  fi
  exit "${status}"
}

for guarded_path in \
  "${source_jail}" \
  "${restore_jail}" \
  "${state_root}" \
  "${snapshot_root}"; do
  case "${guarded_path}" in
    /srv/jailer/firecracker/snapshot-smoke-*|/var/lib/fc/snapshot-smoke|/srv/fc/snapshots/smoke) ;;
    *)
      echo "Refusing to reset unexpected snapshot test path: ${guarded_path}" >&2
      exit 1
      ;;
  esac
done

trap cleanup EXIT
trap report_unexpected_failure ERR
cleanup
install -d -o root -g root -m 0755 \
  "${state_root}" \
  "$(dirname "${volume_path}")" \
  "$(dirname "${snapshot_root}")"

create_volume() {
  local mount_dir
  mount_dir="$(mktemp -d /var/tmp/snapshot-volume.XXXXXX)"
  truncate -s 1G "${volume_path}"
  mkfs.ext4 -q -F -O '^orphan_file' -L agent-data "${volume_path}"
  mount -o loop "${volume_path}" "${mount_dir}"
  install -d -o "${AGENT_UID}" -g "${AGENT_GID}" -m 0755 \
    "${mount_dir}/workspace" \
    "${mount_dir}/pi-state" \
    "${mount_dir}/pi-state/agent"
  printf '%s\n' "${marker}" > "${mount_dir}/workspace/snapshot.marker"
  chown "${AGENT_UID}:${AGENT_GID}" "${mount_dir}/workspace/snapshot.marker"
  sync
  umount "${mount_dir}"
  rmdir "${mount_dir}"
  chown "${firecracker_uid}:${firecracker_gid}" "${volume_path}"
  chmod 0600 "${volume_path}"
}

prepare_test_rootfs() {
  local check_status
  cp --reflink=auto --sparse=always "${rootfs_path}" "${test_rootfs}"
  set +e
  e2fsck -f -y "${test_rootfs}" >/dev/null
  check_status=$?
  set -e
  if [[ "${check_status}" -gt 1 ]]; then
    echo "Snapshot test rootfs filesystem check failed: ${check_status}" >&2
    return 1
  fi
  chmod 0644 "${test_rootfs}"
}

setup_network() {
  cleanup_network
  ip tuntap add dev "${tap_name}" mode tap user "${firecracker_uid}"
  ip address add "${host_cidr}" dev "${tap_name}"
  ip link set dev "${tap_name}" up
  nft -f - <<EOF
table inet ${nft_table} {
  chain input {
    type filter hook input priority -10; policy accept;
    iifname "${tap_name}" ip saddr ${guest_ip} ip daddr ${host_ip} ct state established,related counter accept
    iifname "${tap_name}" ip saddr ${guest_ip} counter drop
  }
  chain forward {
    type filter hook forward priority -10; policy accept;
    iifname "${tap_name}" counter drop
    oifname "${tap_name}" counter drop
  }
}
EOF
}

prepare_source_jail() {
  install -d -o root -g root -m 0755 "${source_root}"
  ln "${kernel_path}" "${source_root}/vmlinux"
  ln "${test_rootfs}" "${source_root}/rootfs.ext4"
  ln "${volume_path}" "${source_root}/data.ext4"
  cat > "${source_root}/config.json" <<EOF
{
  "boot-source": {
    "kernel_image_path": "/vmlinux",
    "boot_args": "root=/dev/vda ro rootfstype=ext4 console=ttyS0 reboot=k panic=1 pci=off init=/usr/local/sbin/agent-init agent_data_disk=1 agent_managed_runtime=1 agent_ip=${guest_cidr} agent_gateway=${host_ip} agent_dns=${host_ip} agent_rpc_port=${rpc_port}"
  },
  "machine-config": {
    "vcpu_count": 2,
    "mem_size_mib": 1024,
    "smt": false,
    "track_dirty_pages": false,
    "huge_pages": "None"
  },
  "drives": [
    {"drive_id":"rootfs","path_on_host":"/rootfs.ext4","is_root_device":true,"is_read_only":true},
    {"drive_id":"data","path_on_host":"/data.ext4","is_root_device":false,"is_read_only":false}
  ],
  "network-interfaces": [
    {"iface_id":"eth0","guest_mac":"${guest_mac}","host_dev_name":"${tap_name}"}
  ],
  "cpu-config": null,
  "balloon": null,
  "vsock": null,
  "logger": null,
  "metrics": null,
  "mmds-config": null,
  "entropy": null,
  "pmem": [],
  "memory-hotplug": null
}
EOF
  chmod 0644 "${source_root}/config.json"
  install -d -o "${firecracker_uid}" -g "${firecracker_gid}" -m 0700 \
    "${source_root}/snapshot"
}

start_jailed_firecracker() {
  local instance_id="$1"
  local console_path="$2"
  local with_config="$3"
  local -a firecracker_arguments
  firecracker_arguments=(--api-sock /firecracker.socket)
  if [[ "${with_config}" == true ]]; then
    firecracker_arguments+=(--config-file /config.json)
  fi

  : > "${console_path}"
  env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    /usr/local/bin/jailer \
      --id "${instance_id}" \
      --exec-file /usr/local/bin/firecracker \
      --uid "${firecracker_uid}" \
      --gid "${firecracker_gid}" \
      --chroot-base-dir "${jailer_base}" \
      --cgroup-version 2 \
      --resource-limit no-file=2048 \
      -- \
      "${firecracker_arguments[@]}" \
      > "${console_path}" 2>&1 &
  current_pid=$!
}

wait_for_socket() {
  local socket_path="$1"
  for _ in $(seq 1 100); do
    if [[ -S "${socket_path}" ]]; then
      return 0
    fi
    if ! kill -0 "${current_pid}" 2>/dev/null; then
      return 1
    fi
    sleep 0.05
  done
  return 1
}

firecracker_api() {
  local socket_path="$1"
  local method="$2"
  local route="$3"
  local body="$4"
  curl --fail --silent --show-error \
    --unix-socket "${socket_path}" \
    --request "${method}" \
    --header 'Accept: application/json' \
    --header 'Content-Type: application/json' \
    --data "${body}" \
    "http://localhost${route}" >/dev/null
}

check_pi_rpc() {
  python3 - "${guest_ip}" "${rpc_port}" <<'PY'
import json
import socket
import sys
import time
import uuid

host, raw_port = sys.argv[1:]
port = int(raw_port)
connect_started = time.monotonic()
deadline = time.monotonic() + 20
last_error = None
while time.monotonic() < deadline:
    try:
        connection = socket.create_connection((host, port), timeout=1)
        break
    except OSError as error:
        last_error = error
        time.sleep(0.1)
else:
    raise SystemExit(f"Pi RPC did not accept connections: {last_error}")
connected_at = time.monotonic()

command_id = f"snapshot-{uuid.uuid4().hex}"
with connection:
    connection.settimeout(15)
    reader = connection.makefile("rb")
    writer = connection.makefile("wb")
    writer.write(json.dumps({"id": command_id, "type": "get_state"}).encode() + b"\n")
    writer.flush()
    while True:
        line = reader.readline()
        if not line:
            raise SystemExit("Pi RPC closed before get_state response")
        record = json.loads(line)
        if record.get("type") == "response" and record.get("id") == command_id:
            if record.get("success") is not True:
                raise SystemExit(f"Pi RPC get_state failed: {record}")
            state = record.get("data")
            if not isinstance(state, dict) or not state.get("sessionId"):
                raise SystemExit("Pi RPC response did not contain a sessionId")
            responded_at = time.monotonic()
            connect_ms = round((connected_at - connect_started) * 1000)
            response_ms = round((responded_at - connected_at) * 1000)
            print(f'{state["sessionId"]}\t{connect_ms}\t{response_ms}')
            break
PY
}

verify_volume_marker() {
  local mount_dir stored check_status
  mount_dir="$(mktemp -d /var/tmp/snapshot-verify.XXXXXX)"
  set +e
  e2fsck -f -y "${volume_path}" >/dev/null
  check_status=$?
  set -e
  if [[ "${check_status}" -gt 1 ]]; then
    echo "Snapshot data volume filesystem check failed: ${check_status}" >&2
    rmdir "${mount_dir}"
    return 1
  fi
  mount -o loop,ro "${volume_path}" "${mount_dir}"
  stored="$(head -n 1 "${mount_dir}/workspace/snapshot.marker")"
  umount "${mount_dir}"
  rmdir "${mount_dir}"
  [[ "${stored}" == "${marker}" ]]
}

prepare_test_rootfs
create_volume
setup_network
prepare_source_jail

echo "Starting the source microVM and waiting for Pi RPC..."
cold_started_at="$(date +%s%3N)"
start_jailed_firecracker "${source_id}" "${source_console}" true
source_socket="${source_root}/firecracker.socket"
if ! wait_for_socket "${source_socket}"; then
  echo "FAIL: source Firecracker API socket did not become ready" >&2
  tail -n 160 "${source_console}" >&2
  exit 1
fi
source_ready=false
for _ in $(seq 1 600); do
  if grep -q '^PI_RPC_READY ' "${source_console}" 2>/dev/null; then
    source_ready=true
    break
  fi
  if ! kill -0 "${current_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.1
done
if [[ "${source_ready}" != true ]]; then
  echo "FAIL: source microVM did not reach Pi RPC readiness" >&2
  tail -n 200 "${source_console}" >&2
  exit 1
fi
cold_ready_at="$(date +%s%3N)"
cold_ms="$((cold_ready_at - cold_started_at))"

echo "Measuring the cold Pi RPC response before taking the snapshot..."
cold_closed_before="$(grep -c '^PI_RPC_CONNECTION_CLOSED ' "${source_console}" 2>/dev/null || true)"
IFS=$'\t' read -r cold_session cold_rpc_connect_ms cold_rpc_response_ms < <(check_pi_rpc)
cold_connection_closed=false
for _ in $(seq 1 100); do
  cold_closed_after="$(grep -c '^PI_RPC_CONNECTION_CLOSED ' "${source_console}" 2>/dev/null || true)"
  if [[ "${cold_closed_after}" -gt "${cold_closed_before}" ]]; then
    cold_connection_closed=true
    break
  fi
  sleep 0.05
done
if [[ "${cold_connection_closed}" != true ]]; then
  echo "FAIL: cold Pi RPC process did not release its test connection" >&2
  tail -n 120 "${source_console}" >&2
  exit 1
fi
# PI_RPC_CONNECTION_CLOSED 输出后，监听循环会立即创建下一次 socat 监听。
# 留出一个很短的调度窗口，确保快照保存的是空闲监听状态。
sleep 0.1

echo "Pausing the source microVM and creating a full snapshot..."
firecracker_api "${source_socket}" PATCH /vm '{"state":"Paused"}'
snapshot_started_at="$(date +%s%3N)"
firecracker_api \
  "${source_socket}" PUT /snapshot/create \
  '{"snapshot_type":"Full","snapshot_path":"/snapshot/vmstate.snap","mem_file_path":"/snapshot/memory.snap"}'
snapshot_finished_at="$(date +%s%3N)"
snapshot_create_ms="$((snapshot_finished_at - snapshot_started_at))"

stop_current_vm
install -d -o root -g root -m 0755 "$(dirname "${snapshot_root}")"
mv "${source_root}/snapshot" "${snapshot_root}"
rm -rf --one-file-system "${source_jail}"

if [[ ! -s "${snapshot_root}/vmstate.snap" || ! -s "${snapshot_root}/memory.snap" ]]; then
  echo "FAIL: Firecracker did not create complete snapshot files" >&2
  exit 1
fi
chmod 0444 "${snapshot_root}/vmstate.snap" "${snapshot_root}/memory.snap"

echo "Starting a new jailed Firecracker process and loading the snapshot..."
install -d -o root -g root -m 0755 "${restore_root}"
ln "${test_rootfs}" "${restore_root}/rootfs.ext4"
ln "${volume_path}" "${restore_root}/data.ext4"
install -d -o "${firecracker_uid}" -g "${firecracker_gid}" -m 0700 \
  "${restore_root}/snapshot"
ln "${snapshot_root}/vmstate.snap" "${restore_root}/snapshot/vmstate.snap"
ln "${snapshot_root}/memory.snap" "${restore_root}/snapshot/memory.snap"

restore_started_at="$(date +%s%3N)"
start_jailed_firecracker "${restore_id}" "${restore_console}" false
restore_socket="${restore_root}/firecracker.socket"
if ! wait_for_socket "${restore_socket}"; then
  echo "FAIL: restore Firecracker API socket did not become ready" >&2
  tail -n 160 "${restore_console}" >&2
  exit 1
fi
process_ready_at="$(date +%s%3N)"
firecracker_process_ms="$((process_ready_at - restore_started_at))"

snapshot_load_started_at="$(date +%s%3N)"
firecracker_api \
  "${restore_socket}" PUT /snapshot/load \
  '{"snapshot_path":"/snapshot/vmstate.snap","mem_backend":{"backend_path":"/snapshot/memory.snap","backend_type":"File"},"track_dirty_pages":false,"resume_vm":false}'
snapshot_load_finished_at="$(date +%s%3N)"
snapshot_load_ms="$((snapshot_load_finished_at - snapshot_load_started_at))"

resume_started_at="$(date +%s%3N)"
firecracker_api "${restore_socket}" PATCH /vm '{"state":"Resumed"}'
resume_finished_at="$(date +%s%3N)"
resume_api_ms="$((resume_finished_at - resume_started_at))"

IFS=$'\t' read -r restored_session rpc_connect_ms rpc_response_ms < <(check_pi_rpc)
restore_ready_at="$(date +%s%3N)"
restore_ms="$((restore_ready_at - restore_started_at))"

stop_current_vm
if ! verify_volume_marker; then
  echo "FAIL: snapshot restore did not preserve the paired data volume" >&2
  exit 1
fi

echo "PASS: Firecracker restored Pi RPC from a full snapshot with its paired data volume"
echo "SNAPSHOT_RESTORE_READY type=full cold_ms=${cold_ms} create_ms=${snapshot_create_ms} restore_ms=${restore_ms} rpc_session=${restored_session} data=preserved network=reconnected"
echo "SNAPSHOT_COLD_RPC_TIMING connect_ms=${cold_rpc_connect_ms} response_ms=${cold_rpc_response_ms} session=${cold_session}"
echo "SNAPSHOT_RESTORE_TIMING process_ms=${firecracker_process_ms} load_ms=${snapshot_load_ms} resume_ms=${resume_api_ms} rpc_connect_ms=${rpc_connect_ms} rpc_response_ms=${rpc_response_ms} total_ms=${restore_ms}"
echo "SNAPSHOT_RPC_COMPARISON cold_response_ms=${cold_rpc_response_ms} restored_response_ms=${rpc_response_ms} delta_ms=$((rpc_response_ms - cold_rpc_response_ms))"
