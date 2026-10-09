#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"

# shellcheck disable=SC1091
. "${repo_dir}/config/versions.env"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script with sudo" >&2
  exit 1
fi

arch="$(uname -m)"
case "${arch}" in
  aarch64)
    ubuntu_arch="arm64"
    node_arch="arm64"
    node_sha256="${NODE_LINUX_ARM64_SHA256}"
    ;;
  x86_64)
    ubuntu_arch="amd64"
    node_arch="x64"
    node_sha256="${NODE_LINUX_X64_SHA256}"
    ;;
  *)
    echo "Unsupported runtime rootfs architecture: ${arch}" >&2
    exit 1
    ;;
esac

artifact_dir="/srv/fc/artifacts"
runtime_dir="/var/lib/fc/runtime"
cache_dir="/var/cache/ai-agent-platform"
rootfs_path="${artifact_dir}/agent-rootfs.ext4"
build_id_path="${artifact_dir}/agent-rootfs.build-id"
manifest_path="${artifact_dir}/agent-rootfs.manifest"
node_archive="node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"
node_cache_path="${cache_dir}/${node_archive}"
pi_archive="pi-coding-agent-${PI_VERSION}.tgz"
pi_cache_path="${cache_dir}/${pi_archive}"
guest_init="${repo_dir}/scripts/guest/agent-init.sh"
guest_task_worker="${repo_dir}/scripts/guest/agent-task-worker.sh"
guest_pi_event_forwarder="${repo_dir}/scripts/guest/pi-event-forwarder.mjs"
guest_workspace_operation="${repo_dir}/scripts/guest/workspace-operation.mjs"

build_id="$(
  {
    printf 'ARCHITECTURE=%s\n' "${arch}"
    sha256sum "${repo_dir}/config/versions.env" | awk '{print $1}'
    sha256sum "${BASH_SOURCE[0]}" | awk '{print $1}'
    sha256sum "${guest_init}" | awk '{print $1}'
    sha256sum "${guest_task_worker}" | awk '{print $1}'
    sha256sum "${guest_pi_event_forwarder}" | awk '{print $1}'
    sha256sum "${guest_workspace_operation}" | awk '{print $1}'
  } | sha256sum | awk '{print $1}'
)"

if [[ -f "${rootfs_path}" && -f "${build_id_path}" && -f "${runtime_dir}/config.json" ]] \
  && [[ "$(<"${build_id_path}")" == "${build_id}" ]]; then
  echo "Verified existing runtime rootfs: ${rootfs_path}"
  echo "Build ID: ${build_id}"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates coreutils curl debootstrap e2fsprogs tar xz-utils

install -d -o root -g root -m 0755 "${artifact_dir}" "${runtime_dir}" "${cache_dir}"

download_checked() {
  url="$1"
  destination="$2"
  expected_sha="$3"
  algorithm="$4"

  if [[ -f "${destination}" ]] \
    && printf '%s  %s\n' "${expected_sha}" "${destination}" \
      | "${algorithm}sum" --check --status; then
    echo "Verified existing download: ${destination}"
    return
  fi

  temporary_file="${destination}.download"
  rm -f "${temporary_file}"
  echo "Downloading ${url}"
  curl --fail --location --proto '=https' --tlsv1.2 \
    --retry 5 --retry-all-errors --retry-delay 2 \
    --output "${temporary_file}" "${url}"
  printf '%s  %s\n' "${expected_sha}" "${temporary_file}" \
    | "${algorithm}sum" --check --status
  install -o root -g root -m 0644 "${temporary_file}" "${destination}"
  rm -f "${temporary_file}"
}

download_checked \
  "https://nodejs.org/dist/v${NODE_VERSION}/${node_archive}" \
  "${node_cache_path}" \
  "${node_sha256}" \
  sha256

download_checked \
  "https://registry.npmjs.org/${PI_PACKAGE}/-/${pi_archive}" \
  "${pi_cache_path}" \
  "${PI_TARBALL_SHA512}" \
  sha512

working_image="${rootfs_path}.building"
mount_dir="$(mktemp -d /var/tmp/agent-rootfs.XXXXXX)"
mounted=false

cleanup() {
  if [[ "${mounted}" == true ]]; then
    umount -R "${mount_dir}" 2>/dev/null || true
  fi
  rmdir "${mount_dir}" 2>/dev/null || true
  rm -f "${working_image}"
}
trap cleanup EXIT

rm -f "${working_image}"
truncate -s "${ROOTFS_SIZE_GIB}G" "${working_image}"
# The smoke-test guest kernel is 5.10, so avoid newer orphan_file metadata.
mkfs.ext4 -q -F -O '^orphan_file' -L agent-rootfs "${working_image}"
mount -o loop "${working_image}" "${mount_dir}"
mounted=true

echo "Bootstrapping Ubuntu ${UBUNTU_RELEASE} ${ubuntu_arch} rootfs..."
bootstrap_complete=false
for bootstrap_attempt in 1 2 3; do
  if debootstrap \
    --arch="${ubuntu_arch}" \
    --variant=minbase \
    "${UBUNTU_RELEASE}" \
    "${mount_dir}" \
    "${UBUNTU_MIRROR}"; then
    bootstrap_complete=true
    break
  fi

  if [[ "${bootstrap_attempt}" -lt 3 ]]; then
    echo "Ubuntu snapshot download failed; retrying debootstrap (${bootstrap_attempt}/3)..." >&2
    umount "${mount_dir}"
    mounted=false
    mkfs.ext4 -q -F -O '^orphan_file' -L agent-rootfs "${working_image}"
    mount -o loop "${working_image}" "${mount_dir}"
    mounted=true
    sleep 3
  fi
done

if [[ "${bootstrap_complete}" != true ]]; then
  echo "Failed to bootstrap Ubuntu after 3 attempts" >&2
  exit 1
fi

printf '%s\n' \
  "deb ${UBUNTU_MIRROR} ${UBUNTU_RELEASE} main universe" \
  "deb ${UBUNTU_MIRROR} ${UBUNTU_RELEASE}-updates main universe" \
  "deb ${UBUNTU_MIRROR} ${UBUNTU_RELEASE}-security main universe" \
  > "${mount_dir}/etc/apt/sources.list"
rm -f "${mount_dir}/etc/resolv.conf"
install -o root -g root -m 0644 /etc/resolv.conf "${mount_dir}/etc/resolv.conf"
printf 'Acquire::Retries "5";\n' > "${mount_dir}/etc/apt/apt.conf.d/80-retries"

install -o root -g root -m 0755 /dev/null "${mount_dir}/usr/sbin/policy-rc.d"
printf '#!/bin/sh\nexit 101\n' > "${mount_dir}/usr/sbin/policy-rc.d"

mount -t proc proc "${mount_dir}/proc"
mount -t sysfs sysfs "${mount_dir}/sys"
mount --rbind /dev "${mount_dir}/dev"
mount --make-rslave "${mount_dir}/dev"

chroot "${mount_dir}" apt-get update
chroot "${mount_dir}" apt-get install -y --no-install-recommends \
  bash ca-certificates curl git iproute2 iputils-ping jq openssh-client \
  procps ripgrep tini util-linux
chroot "${mount_dir}" apt-get clean
rm -rf "${mount_dir}/var/lib/apt/lists/"*

tar -xJf "${node_cache_path}" \
  --strip-components=1 \
  -C "${mount_dir}/usr/local"

install -o root -g root -m 0644 "${pi_cache_path}" "${mount_dir}/tmp/${pi_archive}"
chroot "${mount_dir}" env \
  npm_config_audit=false \
  npm_config_fund=false \
  npm_config_update_notifier=false \
  npm install --global --ignore-scripts --omit=dev "/tmp/${pi_archive}"
rm -f "${mount_dir}/tmp/${pi_archive}"

chroot "${mount_dir}" groupadd --gid "${AGENT_GID}" pi
chroot "${mount_dir}" useradd \
  --uid "${AGENT_UID}" \
  --gid "${AGENT_GID}" \
  --create-home \
  --home-dir /home/pi \
  --shell /bin/bash \
  pi

install -d -o "${AGENT_UID}" -g "${AGENT_GID}" -m 0750 \
  "${mount_dir}/home/pi/.pi/agent"
install -d -o "${AGENT_UID}" -g "${AGENT_GID}" -m 0755 \
  "${mount_dir}/workspace"
install -d -o root -g root -m 0755 \
  "${mount_dir}/mnt/agent-data"
install -o root -g root -m 0755 \
  "${guest_init}" \
  "${mount_dir}/usr/local/sbin/agent-init"
install -o root -g root -m 0755 \
  "${guest_task_worker}" \
  "${mount_dir}/usr/local/sbin/agent-task-worker"
install -o root -g root -m 0755 \
  "${guest_pi_event_forwarder}" \
  "${mount_dir}/usr/local/sbin/pi-event-forwarder.mjs"
install -o root -g root -m 0755 \
  "${guest_workspace_operation}" \
  "${mount_dir}/usr/local/sbin/workspace-operation.mjs"

# Archive metadata must never make the unprivileged Agent user the owner of
# platform runtime files. Only workspace and Pi state are writable by pi.
chown -R 0:0 "${mount_dir}/usr/local"
chmod -R go-w "${mount_dir}/usr/local"

node_actual="$(chroot "${mount_dir}" node --version)"
pi_actual="$(chroot "${mount_dir}" env PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 pi --version)"
uid_actual="$(chroot "${mount_dir}" id -u pi)"

if [[ "${node_actual}" != "v${NODE_VERSION}" ]]; then
  echo "Unexpected Node version: ${node_actual}" >&2
  exit 1
fi
if [[ "${pi_actual}" != *"${PI_VERSION}"* ]]; then
  echo "Unexpected Pi version: ${pi_actual}" >&2
  exit 1
fi
if [[ "${uid_actual}" != "${AGENT_UID}" ]]; then
  echo "Unexpected pi UID: ${uid_actual}" >&2
  exit 1
fi

cat > "${mount_dir}/etc/ai-agent-platform-build" <<EOF
BUILD_ID=${build_id}
UBUNTU_RELEASE=${UBUNTU_RELEASE}
UBUNTU_SNAPSHOT=${UBUNTU_SNAPSHOT}
NODE_VERSION=${NODE_VERSION}
PI_PACKAGE=${PI_PACKAGE}
PI_VERSION=${PI_VERSION}
AGENT_UID=${AGENT_UID}
ARCHITECTURE=${arch}
EOF

chroot "${mount_dir}" dpkg-query -W -f='${Package}\t${Version}\n' \
  | sort > "${manifest_path}.building"
sync
umount -R "${mount_dir}"
mounted=false
set +e
e2fsck -f -y "${working_image}" >/dev/null
filesystem_check_status=$?
set -e
if [[ "${filesystem_check_status}" -gt 1 ]]; then
  echo "Rootfs filesystem check failed with status ${filesystem_check_status}" >&2
  exit "${filesystem_check_status}"
fi

mv "${working_image}" "${rootfs_path}"
printf '%s\n' "${build_id}" > "${build_id_path}"
mv "${manifest_path}.building" "${manifest_path}"
chmod 0644 "${rootfs_path}" "${build_id_path}" "${manifest_path}"

cat > "${runtime_dir}/config.json" <<EOF
{
  "boot-source": {
    "kernel_image_path": "${artifact_dir}/vmlinux-${SMOKE_KERNEL_VERSION}",
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
      "path_on_host": "${rootfs_path}",
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
chmod 0644 "${runtime_dir}/config.json"

echo "Built runtime rootfs: ${rootfs_path}"
echo "Build ID: ${build_id}"
echo "Node: ${node_actual}; Pi: ${pi_actual}; pi UID: ${uid_actual}"
