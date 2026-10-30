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
guest_pi_rpc_session="${repo_dir}/scripts/guest/pi-rpc-session.sh"

build_id="$(
  {
    printf 'ARCHITECTURE=%s\n' "${arch}"
    sha256sum "${repo_dir}/config/versions.env" | awk '{print $1}'
    sha256sum "${BASH_SOURCE[0]}" | awk '{print $1}'
    sha256sum "${guest_init}" | awk '{print $1}'
    sha256sum "${guest_pi_rpc_session}" | awk '{print $1}'
  } | sha256sum | awk '{print $1}'
)"

install_project_runtime_files() {
  root="$1"
  install -d -o "${AGENT_UID}" -g "${AGENT_GID}" -m 0750 \
    "${root}/home/pi/.pi/agent"
  install -d -o "${AGENT_UID}" -g "${AGENT_GID}" -m 0755 \
    "${root}/workspace"
  install -d -o root -g root -m 0755 \
    "${root}/mnt/agent-data"
  install -o root -g root -m 0755 \
    "${guest_init}" \
    "${root}/usr/local/sbin/agent-init"
  install -o root -g root -m 0755 \
    "${guest_pi_rpc_session}" \
    "${root}/usr/local/sbin/pi-rpc-session"

  # 旧 Agent Bridge/JSON Worker/Workspace Worker 已退出正式架构。离线刷新旧镜像时
  # 同步移除残留文件，确保系统盘只保留 Pi RPC 会话入口。
  rm -f \
    "${root}/usr/local/sbin/agent-task-worker" \
    "${root}/usr/local/sbin/pi-event-forwarder.mjs" \
    "${root}/usr/local/sbin/workspace-operation.mjs"

  # 平台运行文件始终由 root 持有；只有工作区与 Pi 状态目录允许 pi 写入。
  chown -R 0:0 "${root}/usr/local"
  chmod -R go-w "${root}/usr/local"
}

verify_static_guest_security() {
  root="$1"
  node_owner="$(stat -c '%u:%g' "${root}/usr/local/bin/node")"
  if [[ "${node_owner}" != "0:0" ]]; then
    echo "Unexpected Node owner in rootfs: ${node_owner}" >&2
    exit 1
  fi
  if chroot "${root}" runuser -u pi -- head -c 1 /etc/shadow >/dev/null 2>&1; then
    echo "The pi user can read /etc/shadow in the rootfs" >&2
    exit 1
  fi
  if chroot "${root}" runuser -u pi -- sh -c \
      ': > /usr/local/.pi-permission-probe' 2>/dev/null; then
    rm -f "${root}/usr/local/.pi-permission-probe"
    echo "The pi user can write /usr/local in the rootfs" >&2
    exit 1
  fi
}

write_build_metadata() {
  root="$1"
  cat > "${root}/etc/ai-agent-platform-build" <<EOF
BUILD_ID=${build_id}
UBUNTU_RELEASE=${UBUNTU_RELEASE}
UBUNTU_SNAPSHOT=${UBUNTU_SNAPSHOT}
NODE_VERSION=${NODE_VERSION}
PI_PACKAGE=${PI_PACKAGE}
PI_VERSION=${PI_VERSION}
AGENT_UID=${AGENT_UID}
ARCHITECTURE=${arch}
STATIC_SECURITY_VERIFIED=1
EOF
}

ensure_clean_rootfs() {
  local check_status repaired_path repair_status
  repaired_path="${rootfs_path}.repairing"

  # 系统盘以只读方式交给 Guest。只读 ext4 无法回放未完成的日志，因此即使
  # Build ID 没有变化，也必须先确认镜像处于可直接只读挂载的干净状态。
  set +e
  e2fsck -f -n "${rootfs_path}" >/dev/null 2>&1
  check_status=$?
  set -e
  if [[ "${check_status}" -le 1 ]]; then
    return 0
  fi

  # 不在公共 inode 上直接修复：它可能仍被正在运行的 Jailer 通过硬链接读取。
  # 修复独立副本后原子替换路径，已有实例继续使用旧 inode，新实例使用干净副本。
  rm -f "${repaired_path}"
  cp --reflink=auto --sparse=always "${rootfs_path}" "${repaired_path}"
  set +e
  e2fsck -f -y "${repaired_path}" >/dev/null
  repair_status=$?
  set -e
  if [[ "${repair_status}" -gt 1 ]]; then
    rm -f "${repaired_path}"
    echo "Rootfs filesystem repair failed with status ${repair_status}" >&2
    return "${repair_status}"
  fi
  chmod 0644 "${repaired_path}"
  mv "${repaired_path}" "${rootfs_path}"
  echo "Repaired runtime rootfs filesystem state: ${rootfs_path}"
}

if [[ -f "${rootfs_path}" && -f "${build_id_path}" && -f "${runtime_dir}/config.json" ]] \
  && [[ "$(<"${build_id_path}")" == "${build_id}" ]]; then
  ensure_clean_rootfs
  echo "Verified existing runtime rootfs: ${rootfs_path}"
  echo "Build ID: ${build_id}"
  exit 0
fi

# Guest脚本变化不应强制重新下载Ubuntu。只要基础系统、Node、Pi和UID仍与
# 当前固定版本一致，就离线更新现有rootfs中的项目文件。基础版本发生变化时
# 才继续执行下面的完整debootstrap流程。
if [[ -f "${rootfs_path}" && -f "${manifest_path}" && -f "${runtime_dir}/config.json" ]] \
  && [[ "$(stat -c '%h' "${rootfs_path}")" == "1" ]]; then
  refresh_mount="$(mktemp -d /var/tmp/agent-rootfs-refresh.XXXXXX)"
  refresh_mounted=false
  cleanup_refresh() {
    if [[ "${refresh_mounted}" == true ]]; then
      umount "${refresh_mount}" 2>/dev/null || true
    fi
    rmdir "${refresh_mount}" 2>/dev/null || true
  }
  trap cleanup_refresh EXIT

  set +e
  e2fsck -f -y "${rootfs_path}" >/dev/null
  refresh_check_status=$?
  set -e
  if [[ "${refresh_check_status}" -le 1 ]]; then
    mount -o loop,rw "${rootfs_path}" "${refresh_mount}"
    refresh_mounted=true
    build_metadata="${refresh_mount}/etc/ai-agent-platform-build"
    if [[ -f "${build_metadata}" ]] \
      && grep -Fxq "UBUNTU_RELEASE=${UBUNTU_RELEASE}" "${build_metadata}" \
      && grep -Fxq "UBUNTU_SNAPSHOT=${UBUNTU_SNAPSHOT}" "${build_metadata}" \
      && grep -Fxq "NODE_VERSION=${NODE_VERSION}" "${build_metadata}" \
      && grep -Fxq "PI_PACKAGE=${PI_PACKAGE}" "${build_metadata}" \
      && grep -Fxq "PI_VERSION=${PI_VERSION}" "${build_metadata}" \
      && grep -Fxq "AGENT_UID=${AGENT_UID}" "${build_metadata}" \
      && grep -Fxq "ARCHITECTURE=${arch}" "${build_metadata}" \
      && [[ -x "${refresh_mount}/usr/bin/socat" ]]; then
      install_project_runtime_files "${refresh_mount}"
      verify_static_guest_security "${refresh_mount}"
      write_build_metadata "${refresh_mount}"
      sync
      umount "${refresh_mount}"
      refresh_mounted=false
      rmdir "${refresh_mount}"
      trap - EXIT
      printf '%s\n' "${build_id}" > "${build_id_path}"
      chmod 0644 "${rootfs_path}" "${build_id_path}" "${manifest_path}"
      echo "Updated project runtime files in existing rootfs: ${rootfs_path}"
      echo "Build ID: ${build_id}"
      exit 0
    fi
    umount "${refresh_mount}"
    refresh_mounted=false
  fi
  rmdir "${refresh_mount}"
  trap - EXIT
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
  bash ca-certificates curl iproute2 iputils-ping jq procps socat tini util-linux
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

install_project_runtime_files "${mount_dir}"
verify_static_guest_security "${mount_dir}"

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

write_build_metadata "${mount_dir}"

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
