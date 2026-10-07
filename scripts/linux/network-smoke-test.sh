#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi

instance_id="network-smoke"
tap_name="fc-tap-smoke"
host_ip_cidr="172.31.254.1/30"
guest_ip="172.31.254.2"
guest_ip_cidr="${guest_ip}/30"
guest_gateway="172.31.254.1"
guest_dns="1.1.1.1"
guest_mac="06:00:ac:1f:fe:02"
guest_subnet="172.31.254.0/30"
firecracker_user="firecracker"
firecracker_group="firecracker"
jailer_base="/srv/jailer"
instance_dir="${jailer_base}/firecracker/${instance_id}"
chroot_dir="${instance_dir}/root"
artifact_dir="/srv/fc/artifacts"
network_dir="/var/lib/fc/network"
console_log="${network_dir}/console.log"
pid_file="${network_dir}/firecracker.pid"
nft_filter_table="ai_agent_smoke"
nft_nat_table="ai_agent_smoke_nat"
kernel_source="$(find "${artifact_dir}" -maxdepth 1 -type f -name 'vmlinux-*' | sort | head -n 1)"
rootfs_source="${artifact_dir}/agent-rootfs.ext4"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends iproute2 nftables

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

egress_interface="$(ip -4 route show default | awk '/default/ {for (i=1; i<=NF; i++) if ($i == "dev") {print $(i+1); exit}}')"
if [[ -z "${egress_interface}" ]]; then
  echo "Could not determine the Linux VM egress interface" >&2
  exit 1
fi

if ! getent group "${firecracker_group}" >/dev/null; then
  groupadd --system "${firecracker_group}"
fi
if ! id "${firecracker_user}" >/dev/null 2>&1; then
  useradd \
    --system \
    --gid "${firecracker_group}" \
    --no-create-home \
    --home-dir /nonexistent \
    --shell /usr/sbin/nologin \
    "${firecracker_user}"
fi
firecracker_uid="$(id -u "${firecracker_user}")"
firecracker_gid="$(id -g "${firecracker_user}")"

install -d -o root -g root -m 0755 "${jailer_base}/firecracker" "${network_dir}"

if [[ -f "${pid_file}" ]]; then
  old_pid="$(<"${pid_file}")"
  if [[ "${old_pid}" =~ ^[0-9]+$ ]] && kill -0 "${old_pid}" 2>/dev/null; then
    echo "A previous network test process is still running with PID ${old_pid}" >&2
    exit 1
  fi
fi

if [[ "${instance_dir}" != "/srv/jailer/firecracker/network-smoke" ]]; then
  echo "Refusing to clean unexpected jail path: ${instance_dir}" >&2
  exit 1
fi

original_ip_forward="$(< /proc/sys/net/ipv4/ip_forward)"
firecracker_pid=""
rules_file="$(mktemp /var/tmp/ai-agent-network.XXXXXX.nft)"

cleanup() {
  if [[ -n "${firecracker_pid}" ]] && kill -0 "${firecracker_pid}" 2>/dev/null; then
    kill "${firecracker_pid}" 2>/dev/null || true
    sleep 1
    kill -9 "${firecracker_pid}" 2>/dev/null || true
  fi
  if [[ -n "${firecracker_pid}" ]]; then
    wait "${firecracker_pid}" 2>/dev/null || true
  fi
  rm -f "${pid_file}" "${rules_file}"
  nft delete table inet "${nft_filter_table}" 2>/dev/null || true
  nft delete table ip "${nft_nat_table}" 2>/dev/null || true
  ip link delete "${tap_name}" 2>/dev/null || true
  printf '%s\n' "${original_ip_forward}" > /proc/sys/net/ipv4/ip_forward
  rm -rf --one-file-system "${instance_dir}"
}
trap cleanup EXIT

nft delete table inet "${nft_filter_table}" 2>/dev/null || true
nft delete table ip "${nft_nat_table}" 2>/dev/null || true
ip link delete "${tap_name}" 2>/dev/null || true
rm -rf --one-file-system "${instance_dir}"

if ip -4 route show exact "${guest_subnet}" | grep -q .; then
  echo "The smoke-test subnet ${guest_subnet} conflicts with an existing route" >&2
  exit 1
fi

ip tuntap add dev "${tap_name}" mode tap user "${firecracker_uid}"
ip address add "${host_ip_cidr}" dev "${tap_name}"
ip link set dev "${tap_name}" up
printf '1\n' > /proc/sys/net/ipv4/ip_forward

cat > "${rules_file}" <<EOF
table inet ${nft_filter_table} {
  chain input {
    type filter hook input priority -10; policy accept;
    iifname "${tap_name}" ip saddr ${guest_ip} ip daddr ${guest_gateway} icmp type echo-request counter accept
    iifname "${tap_name}" ip saddr ${guest_ip} counter drop
  }

  chain forward {
    type filter hook forward priority -10; policy accept;
    iifname "${tap_name}" ip saddr ${guest_ip} ip daddr { 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16 } counter drop
    iifname "${tap_name}" ip saddr ${guest_ip} udp dport 53 ct state new,established counter accept
    iifname "${tap_name}" ip saddr ${guest_ip} tcp dport { 53, 443 } ct state new,established counter accept
    iifname "${tap_name}" ip saddr ${guest_ip} counter drop
    oifname "${tap_name}" ip daddr ${guest_ip} ct state established,related counter accept
    oifname "${tap_name}" counter drop
  }
}

table ip ${nft_nat_table} {
  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    ip saddr ${guest_subnet} oifname "${egress_interface}" masquerade
  }
}
EOF
nft -f "${rules_file}"

install -d -o root -g root -m 0755 "${chroot_dir}"
install -o root -g root -m 0644 "${kernel_source}" "${chroot_dir}/vmlinux"
cp --reflink=auto --sparse=always "${rootfs_source}" "${chroot_dir}/rootfs.ext4"
chown "${firecracker_uid}:${firecracker_gid}" "${chroot_dir}/rootfs.ext4"
chmod 0600 "${chroot_dir}/rootfs.ext4"

cat > "${chroot_dir}/config.json" <<EOF
{
  "boot-source": {
    "kernel_image_path": "/vmlinux",
    "boot_args": "root=/dev/vda rw rootfstype=ext4 console=ttyS0 reboot=k panic=1 pci=off init=/usr/local/sbin/agent-init agent_network_test=1 agent_ip=${guest_ip_cidr} agent_gateway=${guest_gateway} agent_dns=${guest_dns}"
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

rm -f "${console_log}" "${pid_file}"
echo "Starting the jailed microVM with governed egress (75 second test window)..."
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
    > "${console_log}" 2>&1 &
firecracker_pid=$!
printf '%s\n' "${firecracker_pid}" > "${pid_file}"

network_ready=false
for _ in $(seq 1 150); do
  if grep -q '^AGENT_NETWORK_READY ' "${console_log}" 2>/dev/null; then
    network_ready=true
    break
  fi
  if ! kill -0 "${firecracker_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

if [[ "${network_ready}" != true ]]; then
  echo "FAIL: guest network checks did not complete" >&2
  tail -n 200 "${console_log}" >&2
  exit 1
fi

private_drop_packets="$(
  nft list chain inet "${nft_filter_table}" forward \
    | awk '/10\.0\.0\.0\/8.*counter packets/ {for (i=1; i<=NF; i++) if ($i == "packets") {print $(i+1); exit}}'
)"
if [[ -z "${private_drop_packets}" || "${private_drop_packets}" -lt 1 ]]; then
  echo "FAIL: private-network deny rule did not observe blocked traffic" >&2
  nft list chain inet "${nft_filter_table}" forward >&2
  exit 1
fi

echo "PASS: jailed microVM networking, HTTPS egress, and private-network denial checks passed"
grep '^AGENT_NETWORK_READY ' "${console_log}"
echo "NETWORK_POLICY_READY tap=${tap_name} subnet=${guest_subnet} egress=${egress_interface} private_drop_packets=${private_drop_packets} cleanup=armed"
