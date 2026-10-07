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

firecracker_user="firecracker"
firecracker_group="firecracker"
jailer_base="/srv/jailer"
artifact_dir="/srv/fc/artifacts"
volume_dir="/srv/fc/volumes/smoke"
volume_path="${volume_dir}/persistence.ext4"
state_dir="/var/lib/fc/persistence"
kernel_source="$(find "${artifact_dir}" -maxdepth 1 -type f -name 'vmlinux-*' | sort | head -n 1)"
rootfs_source="${artifact_dir}/agent-rootfs.ext4"
token="$(tr -d '-' < /proc/sys/kernel/random/uuid | cut -c 1-16)"
current_pid=""
current_instance_dir=""
volume_mount_dir=""
volume_mounted=false

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

install -d -o root -g root -m 0755 \
  "${jailer_base}/firecracker" "${volume_dir}" "${state_dir}"

stop_current_vm() {
  if [[ -n "${current_pid}" ]] && kill -0 "${current_pid}" 2>/dev/null; then
    kill "${current_pid}" 2>/dev/null || true
    sleep 1
    kill -9 "${current_pid}" 2>/dev/null || true
  fi
  if [[ -n "${current_pid}" ]]; then
    wait "${current_pid}" 2>/dev/null || true
  fi
  current_pid=""
}

cleanup() {
  stop_current_vm
  if [[ "${volume_mounted}" == true ]]; then
    umount "${volume_mount_dir}" 2>/dev/null || true
    volume_mounted=false
  fi
  if [[ -n "${volume_mount_dir}" ]]; then
    rmdir "${volume_mount_dir}" 2>/dev/null || true
  fi
  rm -rf --one-file-system "${jailer_base}/firecracker/persistence-write"
  rm -rf --one-file-system "${jailer_base}/firecracker/persistence-verify"
}

for instance_id in persistence-write persistence-verify; do
  pid_file="${state_dir}/${instance_id}.pid"
  if [[ -f "${pid_file}" ]]; then
    old_pid="$(<"${pid_file}")"
    if [[ "${old_pid}" =~ ^[0-9]+$ ]] && kill -0 "${old_pid}" 2>/dev/null; then
      echo "A previous persistence test process is still running with PID ${old_pid}" >&2
      exit 1
    fi
    rm -f "${pid_file}"
  fi
done

trap cleanup EXIT

rm -rf --one-file-system "${jailer_base}/firecracker/persistence-write"
rm -rf --one-file-system "${jailer_base}/firecracker/persistence-verify"
rm -f "${volume_path}"

truncate -s 1G "${volume_path}"
mkfs.ext4 -q -F -O '^orphan_file' -L agent-data "${volume_path}"
volume_mount_dir="$(mktemp -d /var/tmp/agent-volume.XXXXXX)"
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

run_phase() {
  phase="$1"
  expected_marker="$2"
  instance_id="persistence-${phase}"
  instance_dir="${jailer_base}/firecracker/${instance_id}"
  chroot_dir="${instance_dir}/root"
  console_log="${state_dir}/${phase}.console.log"
  pid_file="${state_dir}/${instance_id}.pid"

  case "${instance_dir}" in
    /srv/jailer/firecracker/persistence-write|/srv/jailer/firecracker/persistence-verify) ;;
    *)
      echo "Refusing to use unexpected jail path: ${instance_dir}" >&2
      exit 1
      ;;
  esac

  rm -rf --one-file-system "${instance_dir}"
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
    echo "Persistent volumes and jailer directories must be on the same filesystem" >&2
    exit 1
  fi

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

  rm -f "${console_log}" "${pid_file}"
  echo "Starting persistence ${phase} phase through jailer..."
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
  current_instance_dir="${instance_dir}"
  printf '%s\n' "${current_pid}" > "${pid_file}"

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
    echo "FAIL: persistence ${phase} phase did not complete" >&2
    tail -n 200 "${console_log}" >&2
    exit 1
  fi

  grep "^${expected_marker} " "${console_log}"
  stop_current_vm
  rm -f "${pid_file}"
  rm -rf --one-file-system "${instance_dir}"
  current_instance_dir=""
}

run_phase write AGENT_PERSISTENCE_WRITTEN

set +e
e2fsck -f -y "${volume_path}" >/dev/null
filesystem_check_status=$?
set -e
if [[ "${filesystem_check_status}" -gt 1 ]]; then
  echo "Persistent volume check failed with status ${filesystem_check_status}" >&2
  exit "${filesystem_check_status}"
fi

run_phase verify AGENT_PERSISTENCE_READY

echo "PASS: read-only rootfs and persistent data volume survived microVM recreation"
echo "PERSISTENCE_POLICY_READY rootfs=readonly data_volume=${volume_path} token=${token} jail=recreated"
