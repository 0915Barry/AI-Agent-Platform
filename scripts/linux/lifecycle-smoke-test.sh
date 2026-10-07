#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"

# shellcheck disable=SC1091
. "${repo_dir}/config/versions.env"
# shellcheck disable=SC1091
. "${repo_dir}/config/lifecycle.env"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi

instance_id="lifecycle-smoke"
tap_name="fc-tap-life"
nft_family="inet"
nft_table="ai_agent_lifecycle"
firecracker_user="firecracker"
firecracker_group="firecracker"
jailer_base="/srv/jailer"
instance_dir="${jailer_base}/firecracker/${instance_id}"
chroot_dir="${instance_dir}/root"
artifact_dir="/srv/fc/artifacts"
volume_dir="/srv/fc/volumes/smoke"
volume_path="${volume_dir}/lifecycle.ext4"
state_root="/var/lib/fc"
state_dir="${state_root}/lifecycle-smoke"
metadata_path="${state_root}/instances/${instance_id}.json"
activity_path="${state_root}/activity/${instance_id}"
busy_path="${state_root}/instances/${instance_id}.busy"
manager="${repo_dir}/services/instance-manager/lifecycle.py"
kernel_source="$(find "${artifact_dir}" -maxdepth 1 -type f -name 'vmlinux-*' | sort | head -n 1)"
rootfs_source="${artifact_dir}/agent-rootfs.ext4"
token="$(tr -d '-' < /proc/sys/kernel/random/uuid | cut -c 1-16)"
smoke_timeout="${LIFECYCLE_SMOKE_IDLE_TIMEOUT_SECONDS}"
current_pid=""
reaper_pid=""
volume_mount_dir=""
volume_mounted=false

if [[ "${smoke_timeout}" != "6" ]]; then
  echo "The first M8 acceptance run must use a 6 second smoke timeout" >&2
  exit 1
fi
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
if [[ ! -f "${manager}" ]]; then
  echo "Lifecycle manager is missing: ${manager}" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends e2fsprogs iproute2 nftables python3

if ! getent group "${firecracker_group}" >/dev/null; then
  groupadd --system "${firecracker_group}"
fi
if ! id "${firecracker_user}" >/dev/null 2>&1; then
  useradd --system --gid "${firecracker_group}" --no-create-home \
    --home-dir /nonexistent --shell /usr/sbin/nologin "${firecracker_user}"
fi
firecracker_uid="$(id -u "${firecracker_user}")"
firecracker_gid="$(id -g "${firecracker_user}")"

install -d -o root -g root -m 0755 \
  "${jailer_base}/firecracker" "${volume_dir}" "${state_dir}" \
  "${state_root}/instances" "${state_root}/activity"

if [[ "${instance_dir}" != "/srv/jailer/firecracker/lifecycle-smoke" ]]; then
  echo "Refusing to use unexpected jail path: ${instance_dir}" >&2
  exit 1
fi

fallback_stop() {
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
  fallback_stop "${reaper_pid}"
  fallback_stop "${current_pid}"
  if [[ "${volume_mounted}" == true ]]; then
    umount "${volume_mount_dir}" 2>/dev/null || true
  fi
  if [[ -n "${volume_mount_dir}" ]]; then
    rmdir "${volume_mount_dir}" 2>/dev/null || true
  fi
  nft delete table "${nft_family}" "${nft_table}" 2>/dev/null || true
  ip link delete "${tap_name}" 2>/dev/null || true
  rm -rf --one-file-system "${instance_dir}"
}
trap cleanup EXIT

if [[ -f "${metadata_path}" ]]; then
  existing_status="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "${metadata_path}")"
  if [[ "${existing_status}" == "running" ]]; then
    python3 "${manager}" --state-root "${state_root}" stop "${instance_id}" --reason replaced-by-smoke-test
  fi
fi
rm -f "${metadata_path}" "${activity_path}" "${busy_path}"
nft delete table "${nft_family}" "${nft_table}" 2>/dev/null || true
ip link delete "${tap_name}" 2>/dev/null || true
rm -rf --one-file-system "${instance_dir}"
rm -f "${volume_path}"

truncate -s 1G "${volume_path}"
mkfs.ext4 -q -F -O '^orphan_file' -L agent-data "${volume_path}"
volume_mount_dir="$(mktemp -d /var/tmp/agent-lifecycle-volume.XXXXXX)"
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

prepare_ephemeral_resources() {
  nft delete table "${nft_family}" "${nft_table}" 2>/dev/null || true
  ip link delete "${tap_name}" 2>/dev/null || true
  rm -rf --one-file-system "${instance_dir}"

  ip tuntap add dev "${tap_name}" mode tap user "${firecracker_uid}"
  ip link set dev "${tap_name}" up
  nft add table "${nft_family}" "${nft_table}"
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
}

start_phase() {
  phase="$1"
  expected_marker="$2"
  console_log="${state_dir}/${phase}.console.log"

  prepare_ephemeral_resources
  cat > "${chroot_dir}/config.json" <<EOF
{
  "boot-source": {
    "kernel_image_path": "/vmlinux",
    "boot_args": "root=/dev/vda ro rootfstype=ext4 console=ttyS0 reboot=k panic=1 pci=off init=/usr/local/sbin/agent-init agent_data_disk=1 agent_persistence_test=1 agent_persistence_phase=${phase} agent_persistence_token=${token}"
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
  "network-interfaces": [],
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
  rm -f "${console_log}"

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
  current_pid=$!

  phase_ready=false
  for _ in $(seq 1 120); do
    if grep -q "^${expected_marker} " "${console_log}" 2>/dev/null; then
      phase_ready=true
      break
    fi
    if ! kill -0 "${current_pid}" 2>/dev/null; then
      break
    fi
    sleep 0.5
  done
  if [[ "${phase_ready}" != true ]]; then
    echo "FAIL: lifecycle ${phase} phase did not reach ${expected_marker}" >&2
    tail -n 200 "${console_log}" >&2
    exit 1
  fi
  grep "^${expected_marker} " "${console_log}"
}

register_current() {
  python3 "${manager}" --state-root "${state_root}" register "${instance_id}" \
    --pid "${current_pid}" \
    --idle-timeout "${smoke_timeout}" \
    --jail-path "${instance_dir}" \
    --api-socket "${chroot_dir}/firecracker.socket" \
    --volume-path "${volume_path}" \
    --tap-name "${tap_name}" \
    --nft-family "${nft_family}" \
    --nft-table "${nft_table}"
}

echo "Starting lifecycle write phase with ${smoke_timeout} second idle timeout..."
start_phase write AGENT_PERSISTENCE_WRITTEN
register_current
python3 "${manager}" --state-root "${state_root}" busy "${instance_id}" on
python3 "${manager}" --state-root "${state_root}" watch "${instance_id}" \
  --poll-interval 0.25 > "${state_dir}/reaper.log" 2>&1 &
reaper_pid=$!

sleep 7
python3 "${manager}" --state-root "${state_root}" status "${instance_id}" \
  | python3 -c 'import json,sys; s=json.load(sys.stdin); assert s["processAlive"] and s["busy"]'

python3 "${manager}" --state-root "${state_root}" heartbeat "${instance_id}"
python3 "${manager}" --state-root "${state_root}" busy "${instance_id}" off
sleep 4
python3 "${manager}" --state-root "${state_root}" heartbeat "${instance_id}"
sleep 3
python3 "${manager}" --state-root "${state_root}" status "${instance_id}" \
  | python3 -c 'import json,sys; s=json.load(sys.stdin); assert s["processAlive"] and not s["busy"]'
echo "INSTANCE_HEARTBEAT_EXTENDED id=${instance_id} timeout=${smoke_timeout} busy=protected heartbeat=extended"

idle_reaped=false
for _ in $(seq 1 40); do
  if grep -q '^INSTANCE_REAPED .*reason=idle_timeout ' "${state_dir}/reaper.log" 2>/dev/null; then
    idle_reaped=true
    break
  fi
  sleep 0.5
done
if [[ "${idle_reaped}" != true ]]; then
  echo "FAIL: lifecycle manager did not reap the idle instance" >&2
  cat "${state_dir}/reaper.log" >&2
  exit 1
fi
wait "${reaper_pid}"
reaper_pid=""
wait "${current_pid}" 2>/dev/null || true
current_pid=""

python3 "${manager}" --state-root "${state_root}" status "${instance_id}" \
  | python3 -c 'import json,sys; s=json.load(sys.stdin); assert s["status"] == "stopped" and s["stopReason"] == "idle_timeout" and not s["processAlive"]'
if ip link show "${tap_name}" >/dev/null 2>&1; then
  echo "FAIL: idle reaper left TAP ${tap_name} behind" >&2
  exit 1
fi
if nft list table "${nft_family}" "${nft_table}" >/dev/null 2>&1; then
  echo "FAIL: idle reaper left nftables table ${nft_table} behind" >&2
  exit 1
fi
if [[ -e "${instance_dir}" ]]; then
  echo "FAIL: idle reaper left jail ${instance_dir} behind" >&2
  exit 1
fi
if [[ ! -f "${volume_path}" ]]; then
  echo "FAIL: idle reaper deleted the persistent data volume" >&2
  exit 1
fi
cat "${state_dir}/reaper.log"
echo "INSTANCE_IDLE_REAP_READY id=${instance_id} timeout=${smoke_timeout}s ephemeral=cleaned volume=preserved"

set +e
e2fsck -f -y "${volume_path}" >/dev/null
filesystem_check_status=$?
set -e
if [[ "${filesystem_check_status}" -gt 1 ]]; then
  echo "Persistent volume check failed with status ${filesystem_check_status}" >&2
  exit "${filesystem_check_status}"
fi

echo "Restarting the instance with the preserved data volume..."
start_phase verify AGENT_PERSISTENCE_READY
register_current
python3 "${manager}" --state-root "${state_root}" stop "${instance_id}" --reason smoke-test-complete
wait "${current_pid}" 2>/dev/null || true
current_pid=""

if [[ ! -f "${volume_path}" ]]; then
  echo "FAIL: normal stop deleted the persistent data volume" >&2
  exit 1
fi
echo "INSTANCE_RESTART_READY id=${instance_id} data=preserved stop=clean"

python3 "${manager}" --state-root "${state_root}" destroy "${instance_id}"
if [[ -e "${volume_path}" || -e "${metadata_path}" || -e "${activity_path}" ]]; then
  echo "FAIL: explicit destroy did not remove lifecycle test data" >&2
  exit 1
fi

echo "PASS: lifecycle manager extended active sessions, reaped idle compute, and preserved restart data"
echo "LIFECYCLE_POLICY_READY default_idle=${INSTANCE_IDLE_TIMEOUT_SECONDS}s smoke_idle=${smoke_timeout}s busy=protected heartbeat=extended ephemeral=cleaned restart_data=preserved explicit_destroy=verified"
