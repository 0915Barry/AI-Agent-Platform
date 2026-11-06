#!/usr/bin/env bash
# 预热池阶段1技术探针：验证「空白 Guest 等待 → PATCH /drives/data 热切盘 →
# 一次性激活挂载 → Pi RPC READY」，并覆盖错误 token/UUID/重复激活/响应丢失。
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"

# shellcheck disable=SC1091
. "${repo_dir}/config/versions.env"

instance_id="prewarm-smoke"
jailer_base="/srv/jailer"
jail="${jailer_base}/firecracker/${instance_id}"
jail_root="${jail}/root"
state_root="/var/lib/fc/prewarm-smoke"
placeholder_path="${state_root}/placeholder.ext4"
volume_path="${state_root}/volume.ext4"
kernel_path="/srv/fc/artifacts/vmlinux-${SMOKE_KERNEL_VERSION}"
rootfs_path="/srv/fc/artifacts/agent-rootfs.ext4"
console_path="${state_root}/console.log"
tap_name="fcprewarm"
nft_family="inet"
nft_table="aap_prewarm"
host_ip="172.31.248.1"
host_cidr="${host_ip}/30"
guest_ip="172.31.248.2"
guest_cidr="${guest_ip}/30"
guest_mac="06:00:ac:1f:f8:02"
rpc_port="8080"
activate_port="8081"
claim_token="prewarm-token-$RANDOM$RANDOM"
activation_id="activation-$RANDOM$RANDOM"
marker="PREWARM_DATA_$RANDOM$RANDOM"
current_pid=""

for required in \
  /usr/local/bin/firecracker \
  /usr/local/bin/jailer \
  "${kernel_path}" \
  "${rootfs_path}"; do
  if [[ ! -e "${required}" ]]; then
    echo "Required prewarm test asset is missing: ${required}" >&2
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
  rm -rf --one-file-system "${jail}"
  rm -rf --one-file-system "${state_root}"
}

report_unexpected_failure() {
  local status="$?"
  trap - ERR
  echo "FAIL: prewarm smoke test stopped unexpectedly (status ${status})" >&2
  if [[ -s "${console_path}" ]]; then
    echo "--- guest console tail ---" >&2
    tail -n 160 "${console_path}" >&2 || true
  fi
  exit "${status}"
}

for guarded_path in "${jail}" "${state_root}"; do
  case "${guarded_path}" in
    /srv/jailer/firecracker/prewarm-smoke|/var/lib/fc/prewarm-smoke) ;;
    *)
      echo "Refusing to reset unexpected prewarm test path: ${guarded_path}" >&2
      exit 1
      ;;
  esac
done

trap cleanup EXIT
trap report_unexpected_failure ERR
cleanup
install -d -o root -g root -m 0755 "${state_root}"

# 占位盘：只预留 1 GiB 容量与可写设备属性，不创建 ext4 文件系统；预热等待期
# 不得被 mount、fsck 或任何探测当成业务盘。
truncate -s 1G "${placeholder_path}"
chown "${firecracker_uid}:${firecracker_gid}" "${placeholder_path}"
chmod 0600 "${placeholder_path}"

# 测试用户卷：真实 ext4，包含 workspace/pi-state 与一个数据标记。
create_volume() {
  local mount_dir
  mount_dir="$(mktemp -d /var/tmp/prewarm-volume.XXXXXX)"
  truncate -s 1G "${volume_path}"
  mkfs.ext4 -q -F -O '^orphan_file' -L agent-data "${volume_path}"
  mount -o loop "${volume_path}" "${mount_dir}"
  install -d -o "${AGENT_UID}" -g "${AGENT_GID}" -m 0755 \
    "${mount_dir}/workspace" \
    "${mount_dir}/pi-state" \
    "${mount_dir}/pi-state/agent"
  printf '%s\n' "${marker}" > "${mount_dir}/workspace/prewarm.marker"
  chown "${AGENT_UID}:${AGENT_GID}" "${mount_dir}/workspace/prewarm.marker"
  sync
  umount "${mount_dir}"
  rmdir "${mount_dir}"
  chown "${firecracker_uid}:${firecracker_gid}" "${volume_path}"
  chmod 0600 "${volume_path}"
}
create_volume

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

prepare_jail() {
  install -d -o root -g root -m 0755 "${jail_root}"
  ln "${kernel_path}" "${jail_root}/vmlinux"
  ln "${rootfs_path}" "${jail_root}/rootfs.ext4"
  ln "${placeholder_path}" "${jail_root}/placeholder.ext4"
  ln "${volume_path}" "${jail_root}/data.ext4"
  cat > "${jail_root}/config.json" <<EOF
{
  "boot-source": {
    "kernel_image_path": "/vmlinux",
    "boot_args": "root=/dev/vda ro rootfstype=ext4 console=ttyS0 reboot=k panic=1 pci=off init=/usr/local/sbin/agent-init agent_prewarm=1 agent_ip=${guest_cidr} agent_gateway=${host_ip} agent_dns=${host_ip} agent_rpc_port=${rpc_port} agent_activate_port=${activate_port} agent_prewarm_token=${claim_token}"
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
    {"drive_id":"data","path_on_host":"/placeholder.ext4","is_root_device":false,"is_read_only":false}
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
  chmod 0644 "${jail_root}/config.json"
}

start_jailed_firecracker() {
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
      --api-sock /firecracker.socket \
      --config-file /config.json \
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

# 通过 TCP 向激活入口发送一行固定协议，返回一行响应。
send_activate() {
  local line="$1"
  python3 - "${guest_ip}" "${activate_port}" "${line}" <<'PY'
import socket
import sys

host, port, line = sys.argv[1], int(sys.argv[2]), sys.argv[3]
with socket.create_connection((host, port), timeout=5) as connection:
    connection.sendall((line + "\n").encode())
    connection.settimeout(5)
    response = connection.makefile("rb").readline().decode().strip()
    print(response)
PY
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

command_id = f"prewarm-{uuid.uuid4().hex}"
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
  mount_dir="$(mktemp -d /var/tmp/prewarm-verify.XXXXXX)"
  # 硬杀后 ext4 可能带脏日志，只读挂载无法回放；先离线修复再只读校验。
  set +e
  e2fsck -f -y "${volume_path}" >/dev/null
  check_status=$?
  set -e
  if [[ "${check_status}" -gt 1 ]]; then
    echo "prewarm data volume filesystem check failed: ${check_status}" >&2
    rmdir "${mount_dir}"
    return 1
  fi
  mount -o loop,ro "${volume_path}" "${mount_dir}"
  stored="$(head -n 1 "${mount_dir}/workspace/prewarm.marker")"
  umount "${mount_dir}"
  rmdir "${mount_dir}"
  [[ "${stored}" == "${marker}" ]]
}

volume_uuid="$(blkid -s UUID -o value "${volume_path}")"

setup_network
prepare_jail

echo "Starting the blank prewarm microVM and waiting for WARM_READY..."
start_jailed_firecracker
jail_socket="${jail_root}/firecracker.socket"
if ! wait_for_socket "${jail_socket}"; then
  echo "FAIL: prewarm Firecracker API socket did not become ready" >&2
  tail -n 160 "${console_path}" >&2
  exit 1
fi
warm_ready=false
for _ in $(seq 1 600); do
  if grep -q '^WARM_READY ' "${console_path}" 2>/dev/null; then
    warm_ready=true
    break
  fi
  if ! kill -0 "${current_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.1
done
if [[ "${warm_ready}" != true ]]; then
  echo "FAIL: blank guest did not reach WARM_READY" >&2
  tail -n 200 "${console_path}" >&2
  exit 1
fi
echo "PREWARM_WARM_READY id=${instance_id} activate_address=${guest_ip}:${activate_port}"

# 占位盘仍是无文件系统的空盘：PROBE 应报告 warm，错误 token 应被拒绝。
probe_response="$(send_activate "PROBE_V1 ${claim_token}")"
if [[ "${probe_response}" != "PROBE_READY state=warm" ]]; then
  echo "FAIL: PROBE_V1 did not report warm state: ${probe_response}" >&2
  exit 1
fi

bad_token_response="$(send_activate "ACTIVATE_V1 wrong-token ${activation_id} ${volume_uuid}")"
if [[ "${bad_token_response}" != "ACTIVATE_REJECTED reason=bad_token" ]]; then
  echo "FAIL: wrong token was not rejected: ${bad_token_response}" >&2
  exit 1
fi
echo "PREWARM_REJECTIONS_READY bad_token=rejected probe=warm"

echo "Swapping the data backend from placeholder to the test volume via PATCH..."
firecracker_api "${jail_socket}" PATCH /drives/data \
  '{"drive_id":"data","path_on_host":"/data.ext4"}'

# 后端已切到真实测试卷：错误 UUID 必须被拒绝（防止误挂到错误用户卷）。
bad_uuid_response="$(send_activate "ACTIVATE_V1 ${claim_token} ${activation_id} 00000000-0000-0000-0000-000000000000")"
if [[ "${bad_uuid_response}" != "ACTIVATE_REJECTED reason=volume_uuid_mismatch" ]]; then
  echo "FAIL: wrong volume UUID was not rejected: ${bad_uuid_response}" >&2
  exit 1
fi
echo "PREWARM_UUID_CHECK_READY bad_uuid=rejected"

echo "Activating the guest with the test volume..."
activate_response="$(send_activate "ACTIVATE_V1 ${claim_token} ${activation_id} ${volume_uuid}")"
if [[ "${activate_response}" != "BOUND_READY activation_id=${activation_id} rpc_address=${guest_ip}:${rpc_port}" ]]; then
  echo "FAIL: activation did not report BOUND_READY: ${activate_response}" >&2
  tail -n 200 "${console_path}" >&2
  exit 1
fi
echo "PREWARM_BOUND_READY activation_id=${activation_id} rpc_address=${guest_ip}:${rpc_port}"

# 响应丢失后的重连语义：状态保存在 /run，重复激活被拒绝，PROBE 报告 activated。
duplicate_response="$(send_activate "ACTIVATE_V1 ${claim_token} ${activation_id} ${volume_uuid}")"
if [[ "${duplicate_response}" != "ACTIVATE_REJECTED reason=already_activated" ]]; then
  echo "FAIL: duplicate activation was not rejected: ${duplicate_response}" >&2
  exit 1
fi
activated_probe="$(send_activate "PROBE_V1 ${claim_token}")"
if [[ "${activated_probe}" != "PROBE_READY state=activated" ]]; then
  echo "FAIL: PROBE_V1 after activation did not report activated: ${activated_probe}" >&2
  exit 1
fi
echo "PREWARM_RECONNECT_READY duplicate=rejected state=activated"

IFS=$'\t' read -r pi_session rpc_connect_ms rpc_response_ms < <(check_pi_rpc)
echo "PREWARM_PI_RPC_READY session=${pi_session} connect_ms=${rpc_connect_ms} response_ms=${rpc_response_ms}"

stop_current_vm
if ! verify_volume_marker; then
  echo "FAIL: activated volume did not preserve the test marker" >&2
  exit 1
fi

echo "PASS: blank guest swapped data backend via PATCH and activated to a working Pi RPC"
echo "PREWARM_SMOKE_READY warm=verified patch=verified activate=verified rpc=verified data=preserved rejections=bad_token,bad_uuid,duplicate"
