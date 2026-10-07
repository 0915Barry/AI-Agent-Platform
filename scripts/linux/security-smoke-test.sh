#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi

instance_id="security-smoke"
firecracker_user="firecracker"
firecracker_group="firecracker"
jailer_base="/srv/jailer"
instance_dir="${jailer_base}/firecracker/${instance_id}"
chroot_dir="${instance_dir}/root"
artifact_dir="/srv/fc/artifacts"
security_dir="/var/lib/fc/security"
console_log="${security_dir}/console.log"
pid_file="${security_dir}/firecracker.pid"
kernel_source="$(find "${artifact_dir}" -maxdepth 1 -type f -name 'vmlinux-*' | sort | head -n 1)"
rootfs_source="${artifact_dir}/agent-rootfs.ext4"

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

install -d -o root -g root -m 0755 "${jailer_base}/firecracker" "${security_dir}"

if [[ -f "${pid_file}" ]]; then
  old_pid="$(<"${pid_file}")"
  if [[ "${old_pid}" =~ ^[0-9]+$ ]] && kill -0 "${old_pid}" 2>/dev/null; then
    echo "A previous security test process is still running with PID ${old_pid}" >&2
    exit 1
  fi
fi

if [[ "${instance_dir}" != "/srv/jailer/firecracker/security-smoke" ]]; then
  echo "Refusing to clean unexpected jail path: ${instance_dir}" >&2
  exit 1
fi
rm -rf --one-file-system "${instance_dir}"
install -d -o root -g root -m 0755 "${chroot_dir}"

install -o root -g root -m 0644 "${kernel_source}" "${chroot_dir}/vmlinux"
cp --reflink=auto --sparse=always "${rootfs_source}" "${chroot_dir}/rootfs.ext4"
chown "${firecracker_uid}:${firecracker_gid}" "${chroot_dir}/rootfs.ext4"
chmod 0600 "${chroot_dir}/rootfs.ext4"

cat > "${chroot_dir}/config.json" <<'EOF'
{
  "boot-source": {
    "kernel_image_path": "/vmlinux",
    "boot_args": "root=/dev/vda rw rootfstype=ext4 console=ttyS0 reboot=k panic=1 pci=off init=/usr/local/sbin/agent-init"
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
echo "Starting Firecracker through jailer (60 second test window)..."
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

cleanup() {
  if kill -0 "${firecracker_pid}" 2>/dev/null; then
    kill "${firecracker_pid}" 2>/dev/null || true
    sleep 1
    kill -9 "${firecracker_pid}" 2>/dev/null || true
  fi
  wait "${firecracker_pid}" 2>/dev/null || true
  rm -f "${pid_file}"
  rm -rf --one-file-system "${instance_dir}"
}
trap cleanup EXIT

guest_ready=false
for _ in $(seq 1 120); do
  if grep -q '^AGENT_SECURITY_READY ' "${console_log}" 2>/dev/null; then
    guest_ready=true
    break
  fi
  if ! kill -0 "${firecracker_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

if [[ "${guest_ready}" != true ]]; then
  echo "FAIL: jailed guest did not pass its permission checks" >&2
  tail -n 180 "${console_log}" >&2
  exit 1
fi

status_file="/proc/${firecracker_pid}/status"
limits_file="/proc/${firecracker_pid}/limits"
environment_file="/proc/${firecracker_pid}/environ"

if [[ ! -r "${status_file}" ]]; then
  echo "FAIL: jailed Firecracker process is not available for inspection" >&2
  exit 1
fi

actual_uid="$(awk '/^Uid:/ {print $2}' "${status_file}")"
actual_gid="$(awk '/^Gid:/ {print $2}' "${status_file}")"
seccomp_mode="$(awk '/^Seccomp:/ {print $2}' "${status_file}")"
cap_effective="$(awk '/^CapEff:/ {print $2}' "${status_file}")"
cap_permitted="$(awk '/^CapPrm:/ {print $2}' "${status_file}")"
environment_count="$(tr '\0' '\n' < "${environment_file}" | sed '/^$/d' | wc -l | tr -d '[:space:]')"
nofile_soft="$(awk '/^Max open files/ {print $4}' "${limits_file}")"

[[ "${actual_uid}" == "${firecracker_uid}" ]] || { echo "FAIL: unexpected VMM uid ${actual_uid}" >&2; exit 1; }
[[ "${actual_gid}" == "${firecracker_gid}" ]] || { echo "FAIL: unexpected VMM gid ${actual_gid}" >&2; exit 1; }
[[ "${seccomp_mode}" == "2" ]] || { echo "FAIL: seccomp mode is ${seccomp_mode}" >&2; exit 1; }
[[ "${cap_effective}" == "0000000000000000" ]] || { echo "FAIL: effective capabilities are ${cap_effective}" >&2; exit 1; }
[[ "${cap_permitted}" == "0000000000000000" ]] || { echo "FAIL: permitted capabilities are ${cap_permitted}" >&2; exit 1; }
[[ "${environment_count}" == "0" ]] || { echo "FAIL: VMM inherited ${environment_count} environment variables" >&2; exit 1; }
[[ "${nofile_soft}" == "2048" ]] || { echo "FAIL: nofile soft limit is ${nofile_soft}" >&2; exit 1; }

thread_count=0
for thread_status in "/proc/${firecracker_pid}/task/"*/status; do
  thread_seccomp="$(awk '/^Seccomp:/ {print $2}' "${thread_status}")"
  thread_cap_effective="$(awk '/^CapEff:/ {print $2}' "${thread_status}")"
  thread_cap_permitted="$(awk '/^CapPrm:/ {print $2}' "${thread_status}")"
  [[ "${thread_seccomp}" == "2" ]] || { echo "FAIL: a VMM thread is not under seccomp" >&2; exit 1; }
  [[ "${thread_cap_effective}" == "0000000000000000" ]] || { echo "FAIL: a VMM thread has effective capabilities" >&2; exit 1; }
  [[ "${thread_cap_permitted}" == "0000000000000000" ]] || { echo "FAIL: a VMM thread has permitted capabilities" >&2; exit 1; }
  thread_count=$((thread_count + 1))
done

for forbidden_path in etc home usr bin; do
  if [[ -e "/proc/${firecracker_pid}/root/${forbidden_path}" ]]; then
    echo "FAIL: jailed VMM can see /${forbidden_path}" >&2
    exit 1
  fi
done

[[ -c "${chroot_dir}/dev/kvm" ]] || { echo "FAIL: jailed /dev/kvm is missing" >&2; exit 1; }
[[ -c "${chroot_dir}/dev/net/tun" ]] || { echo "FAIL: jailed /dev/net/tun is missing" >&2; exit 1; }

echo "PASS: guest permissions and Firecracker jailer hardening checks passed"
grep '^AGENT_SECURITY_READY ' "${console_log}"
echo "JAILER_SECURITY_READY uid=${actual_uid} gid=${actual_gid} seccomp=all:${thread_count} capabilities=0 environment=0 nofile=${nofile_soft} chroot=isolated devices=kvm,tun"
