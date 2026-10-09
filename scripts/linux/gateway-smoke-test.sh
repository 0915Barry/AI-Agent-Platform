#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"

# shellcheck disable=SC1091
. "${repo_dir}/config/versions.env"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi

instance_id="gateway-smoke"
tap_name="fc-tap-gw"
host_ip="172.31.253.1"
host_ip_cidr="${host_ip}/30"
guest_ip="172.31.253.2"
guest_ip_cidr="${guest_ip}/30"
guest_subnet="172.31.253.0/30"
guest_mac="06:00:ac:1f:fd:02"
gateway_port="18080"
upstream_port="18081"
nft_table="ai_agent_gateway_smoke"
firecracker_user="firecracker"
firecracker_group="firecracker"
gateway_user="agent-gateway"
gateway_group="agent-gateway"
jailer_base="/srv/jailer"
instance_dir="${jailer_base}/firecracker/${instance_id}"
chroot_dir="${instance_dir}/root"
artifact_dir="/srv/fc/artifacts"
volume_dir="/srv/fc/volumes/smoke"
volume_path="${volume_dir}/gateway.ext4"
state_dir="/var/lib/fc/gateway"
credential_path="${state_dir}/credential"
gateway_audit="${state_dir}/gateway.audit.jsonl"
upstream_audit="${state_dir}/upstream.audit.jsonl"
gateway_log="${state_dir}/gateway.log"
upstream_log="${state_dir}/upstream.log"
console_log="${state_dir}/console.log"
firecracker_pid_file="${state_dir}/firecracker.pid"
gateway_pid_file="${state_dir}/gateway.pid"
upstream_pid_file="${state_dir}/upstream.pid"
kernel_source="$(find "${artifact_dir}" -maxdepth 1 -type f -name 'vmlinux-*' | sort | head -n 1)"
rootfs_source="${artifact_dir}/agent-rootfs.ext4"
rules_file=""
volume_mount_dir=""
volume_mounted=false
firecracker_pid=""
gateway_pid=""
upstream_pid=""

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends curl iproute2 nftables python3 util-linux

if [[ -z "${kernel_source}" || ! -f "${kernel_source}" ]]; then
  echo "Guest kernel is missing; run prepare-microvm first" >&2
  exit 1
fi
if [[ ! -f "${rootfs_source}" ]]; then
  echo "Runtime rootfs is missing; run build-rootfs first" >&2
  exit 1
fi
if [[ ! -x /usr/local/bin/firecracker || ! -x /usr/local/bin/jailer ]]; then
  echo "Firecracker and jailer must be installed" >&2
  exit 1
fi

if ! getent group "${firecracker_group}" >/dev/null; then
  groupadd --system "${firecracker_group}"
fi
if ! id "${firecracker_user}" >/dev/null 2>&1; then
  useradd --system --gid "${firecracker_group}" --no-create-home \
    --home-dir /nonexistent --shell /usr/sbin/nologin "${firecracker_user}"
fi
if ! getent group "${gateway_group}" >/dev/null; then
  groupadd --system "${gateway_group}"
fi
if ! id "${gateway_user}" >/dev/null 2>&1; then
  useradd --system --gid "${gateway_group}" --no-create-home \
    --home-dir /nonexistent --shell /usr/sbin/nologin "${gateway_user}"
fi

firecracker_uid="$(id -u "${firecracker_user}")"
firecracker_gid="$(id -g "${firecracker_user}")"
gateway_uid="$(id -u "${gateway_user}")"
gateway_gid="$(id -g "${gateway_user}")"

install -d -o root -g root -m 0755 \
  "${jailer_base}/firecracker" "${volume_dir}"
install -d -o "${gateway_uid}" -g "${gateway_gid}" -m 0750 "${state_dir}"

for pid_file in "${firecracker_pid_file}" "${gateway_pid_file}" "${upstream_pid_file}"; do
  if [[ -f "${pid_file}" ]]; then
    old_pid="$(<"${pid_file}")"
    if [[ "${old_pid}" =~ ^[0-9]+$ ]] && kill -0 "${old_pid}" 2>/dev/null; then
      echo "A previous Tool Gateway test process is still running with PID ${old_pid}" >&2
      exit 1
    fi
    rm -f "${pid_file}"
  fi
done

if [[ "${instance_dir}" != "/srv/jailer/firecracker/gateway-smoke" ]]; then
  echo "Refusing to use unexpected jail path: ${instance_dir}" >&2
  exit 1
fi

stop_process() {
  process_pid="$1"
  if [[ -n "${process_pid}" ]] && kill -0 "${process_pid}" 2>/dev/null; then
    kill "${process_pid}" 2>/dev/null || true
    sleep 1
    kill -9 "${process_pid}" 2>/dev/null || true
  fi
  if [[ -n "${process_pid}" ]]; then
    wait "${process_pid}" 2>/dev/null || true
  fi
}

cleanup() {
  stop_process "${firecracker_pid}"
  stop_process "${gateway_pid}"
  stop_process "${upstream_pid}"
  if [[ "${volume_mounted}" == true ]]; then
    umount "${volume_mount_dir}" 2>/dev/null || true
  fi
  if [[ -n "${volume_mount_dir}" ]]; then
    rmdir "${volume_mount_dir}" 2>/dev/null || true
  fi
  rm -f "${firecracker_pid_file}" "${gateway_pid_file}" "${upstream_pid_file}"
  rm -f "${credential_path}"
  if [[ -n "${rules_file}" ]]; then
    rm -f "${rules_file}"
  fi
  nft delete table inet "${nft_table}" 2>/dev/null || true
  ip link delete "${tap_name}" 2>/dev/null || true
  rm -rf --one-file-system "${instance_dir}"
}
trap cleanup EXIT

nft delete table inet "${nft_table}" 2>/dev/null || true
ip link delete "${tap_name}" 2>/dev/null || true
rm -rf --one-file-system "${instance_dir}"

if ip -4 route show exact "${guest_subnet}" | grep -q .; then
  echo "The Tool Gateway test subnet ${guest_subnet} conflicts with an existing route" >&2
  exit 1
fi

ip tuntap add dev "${tap_name}" mode tap user "${firecracker_uid}"
ip address add "${host_ip_cidr}" dev "${tap_name}"
ip link set dev "${tap_name}" up

rules_file="$(mktemp /var/tmp/ai-agent-gateway.XXXXXX.nft)"
cat > "${rules_file}" <<EOF
table inet ${nft_table} {
  chain input {
    type filter hook input priority -10; policy accept;
    iifname "${tap_name}" ip saddr ${guest_ip} ip daddr ${host_ip} tcp dport ${gateway_port} counter accept
    iifname "${tap_name}" ip saddr ${guest_ip} counter drop
  }

  chain forward {
    type filter hook forward priority -10; policy accept;
    iifname "${tap_name}" counter drop
    oifname "${tap_name}" counter drop
  }
}
EOF
nft -f "${rules_file}"

test_credential="gw-smoke-$(tr -d '-' < /proc/sys/kernel/random/uuid)"
printf '%s\n' "${test_credential}" > "${credential_path}"
chown "${gateway_uid}:${gateway_gid}" "${credential_path}"
chmod 0400 "${credential_path}"
: > "${gateway_audit}"
: > "${upstream_audit}"
: > "${gateway_log}"
: > "${upstream_log}"
: > "${console_log}"
chown "${gateway_uid}:${gateway_gid}" "${gateway_audit}" "${upstream_audit}"
chmod 0600 "${gateway_audit}" "${upstream_audit}"
chmod 0640 "${gateway_log}" "${upstream_log}" "${console_log}"

env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  setpriv --reuid "${gateway_uid}" --regid "${gateway_gid}" --clear-groups \
  python3 "${repo_dir}/services/tool-gateway/mock_upstream.py" \
    --listen-host 127.0.0.1 \
    --listen-port "${upstream_port}" \
    --credential-file "${credential_path}" \
    --audit-file "${upstream_audit}" \
    > "${upstream_log}" 2>&1 &
upstream_pid=$!
printf '%s\n' "${upstream_pid}" > "${upstream_pid_file}"

env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  setpriv --reuid "${gateway_uid}" --regid "${gateway_gid}" --clear-groups \
  python3 "${repo_dir}/services/tool-gateway/gateway.py" \
    --listen-host "${host_ip}" \
    --listen-port "${gateway_port}" \
    --upstream "http://127.0.0.1:${upstream_port}" \
    --credential-file "${credential_path}" \
    --audit-file "${gateway_audit}" \
    > "${gateway_log}" 2>&1 &
gateway_pid=$!
printf '%s\n' "${gateway_pid}" > "${gateway_pid_file}"

services_ready=false
for _ in $(seq 1 40); do
  if ss -H -ltn "sport = :${gateway_port}" | grep -q "${host_ip}:${gateway_port}" \
    && ss -H -ltn "sport = :${upstream_port}" | grep -q "127.0.0.1:${upstream_port}"; then
    services_ready=true
    break
  fi
  if ! kill -0 "${gateway_pid}" 2>/dev/null || ! kill -0 "${upstream_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.25
done
if [[ "${services_ready}" != true ]]; then
  echo "FAIL: Tool Gateway services did not start" >&2
  cat "${gateway_log}" "${upstream_log}" >&2
  exit 1
fi
if ss -H -ltn "sport = :${gateway_port}" \
  | awk '{print $4}' \
  | grep -qE '^(0\.0\.0\.0|\*):'; then
  echo "FAIL: Tool Gateway is exposed on a wildcard address" >&2
  exit 1
fi

forbidden_status="$(
  curl --silent --output /dev/null --write-out '%{http_code}' \
    --request POST --header 'Content-Type: application/json' --data '{}' \
    "http://${host_ip}:${gateway_port}/forbidden"
)"
if [[ "${forbidden_status}" != "404" ]]; then
  echo "FAIL: Tool Gateway accepted an unapproved route with status ${forbidden_status}" >&2
  exit 1
fi

# M15：用模拟上游验证 SSE 字节能穿过 Gateway，而不是等完整回答后一次性返回。
stream_response="$(
  curl --fail --silent --show-error --no-buffer --max-time 10 \
    --request POST \
    --header 'Authorization: Bearer guest-placeholder' \
    --header 'Content-Type: application/json' \
    --data '{"model":"gateway-smoke","stream":true,"messages":[{"role":"user","content":"stream"}]}' \
    "http://${host_ip}:${gateway_port}/v1/chat/completions"
)"
for expected_delta in '"content":"gateway-"' '"content":"stream-"' '"content":"ok"' 'data: [DONE]'; do
  if [[ "${stream_response}" != *"${expected_delta}"* ]]; then
    echo "FAIL: Gateway did not forward the complete SSE stream" >&2
    exit 1
  fi
done

rm -f "${volume_path}"
truncate -s 1G "${volume_path}"
mkfs.ext4 -q -F -O '^orphan_file' -L agent-data "${volume_path}"
volume_mount_dir="$(mktemp -d /var/tmp/agent-gateway-volume.XXXXXX)"
mount -o loop "${volume_path}" "${volume_mount_dir}"
volume_mounted=true
install -d -o "${AGENT_UID}" -g "${AGENT_GID}" -m 0755 \
  "${volume_mount_dir}/workspace" "${volume_mount_dir}/pi-state"
sync
umount "${volume_mount_dir}"
volume_mounted=false
rmdir "${volume_mount_dir}"
volume_mount_dir=""
chown "${firecracker_uid}:${firecracker_gid}" "${volume_path}"
chmod 0600 "${volume_path}"

install -d -o root -g root -m 0755 "${chroot_dir}"
if ! ln "${kernel_source}" "${chroot_dir}/vmlinux"; then
  echo "Kernel and jailer directories must be on the same filesystem" >&2
  exit 1
fi
if ! ln "${rootfs_source}" "${chroot_dir}/rootfs.ext4"; then
  echo "Rootfs and jailer directories must be on the same filesystem" >&2
  exit 1
fi
if ! ln "${volume_path}" "${chroot_dir}/data.ext4"; then
  echo "Data volume and jailer directories must be on the same filesystem" >&2
  exit 1
fi

cat > "${chroot_dir}/config.json" <<EOF
{
  "boot-source": {
    "kernel_image_path": "/vmlinux",
    "boot_args": "root=/dev/vda ro rootfstype=ext4 console=ttyS0 reboot=k panic=1 pci=off init=/usr/local/sbin/agent-init agent_data_disk=1 agent_gateway_test=1 agent_ip=${guest_ip_cidr} agent_gateway=${host_ip} agent_dns=${host_ip} agent_gateway_host=${host_ip} agent_gateway_port=${gateway_port} agent_upstream_port=${upstream_port}"
  },
  "machine-config": {
    "vcpu_count": 2,
    "mem_size_mib": 1024,
    "smt": false,
    "track_dirty_pages": false,
    "huge_pages": "None"
  },
  "drives": [
    {
      "drive_id": "rootfs",
      "path_on_host": "/rootfs.ext4",
      "is_root_device": true,
      "is_read_only": true
    },
    {
      "drive_id": "data",
      "path_on_host": "/data.ext4",
      "is_root_device": false,
      "is_read_only": false
    }
  ],
  "network-interfaces": [
    {
      "iface_id": "eth0",
      "guest_mac": "${guest_mac}",
      "host_dev_name": "${tap_name}"
    }
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
chown root:root "${chroot_dir}/config.json"
chmod 0644 "${chroot_dir}/config.json"

echo "Starting the jailed microVM with host-side Tool Gateway (75 second test window)..."
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
    >> "${console_log}" 2>&1 &
firecracker_pid=$!
printf '%s\n' "${firecracker_pid}" > "${firecracker_pid_file}"

guest_ready=false
for _ in $(seq 1 150); do
  if grep -q '^AGENT_GATEWAY_READY ' "${console_log}" 2>/dev/null; then
    guest_ready=true
    break
  fi
  if ! kill -0 "${firecracker_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.5
done
if [[ "${guest_ready}" != true ]]; then
  echo "FAIL: guest did not complete the Tool Gateway request" >&2
  tail -n 220 "${console_log}" >&2
  cat "${gateway_log}" "${upstream_log}" >&2
  exit 1
fi

grep -q '"credential_source":"host_file"' "${gateway_audit}" \
  || { echo "FAIL: gateway audit did not record host-side credential injection" >&2; exit 1; }
grep -q '"status":200' "${gateway_audit}" \
  || { echo "FAIL: gateway audit did not record a successful request" >&2; exit 1; }
grep -q '"stream":true' "${gateway_audit}" \
  || { echo "FAIL: gateway audit did not record streaming mode" >&2; exit 1; }
grep -q '"auth_valid":true' "${upstream_audit}" \
  || { echo "FAIL: mock upstream did not receive the injected credential" >&2; exit 1; }

drop_packets="$(
  nft list chain inet "${nft_table}" input \
    | awk '/iifname "'"${tap_name}"'".*counter packets.*drop/ {for (i=1; i<=NF; i++) if ($i == "packets") {print $(i+1); exit}}'
)"
if [[ -z "${drop_packets}" || "${drop_packets}" -lt 1 ]]; then
  echo "FAIL: direct-to-host deny rule did not observe blocked traffic" >&2
  nft list chain inet "${nft_table}" input >&2
  exit 1
fi

for inspected_file in \
  "${console_log}" "${gateway_audit}" "${upstream_audit}" \
  "${gateway_log}" "${upstream_log}" "${chroot_dir}/config.json"; do
  if grep -Fq -- "${test_credential}" "${inspected_file}"; then
    echo "FAIL: injected credential leaked into ${inspected_file}" >&2
    exit 1
  fi
done

credential_mode="$(stat -c '%a' "${credential_path}")"
credential_owner="$(stat -c '%u:%g' "${credential_path}")"
if [[ "${credential_mode}" != "400" || "${credential_owner}" != "${gateway_uid}:${gateway_gid}" ]]; then
  echo "FAIL: credential file permissions are ${credential_owner} mode=${credential_mode}" >&2
  exit 1
fi

echo "PASS: Tool Gateway injected a host credential without exposing it to the microVM"
grep '^AGENT_GATEWAY_READY ' "${console_log}"
echo "TOOL_GATEWAY_READY bind=${host_ip}:${gateway_port} credential=host-file:0400 upstream=loopback:${upstream_port} audit=redacted direct_upstream=denied drop_packets=${drop_packets}"
