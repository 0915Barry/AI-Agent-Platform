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
    kernel_sha256="${SMOKE_KERNEL_SHA256_AARCH64}"
    initramfs_sha256="${SMOKE_INITRAMFS_SHA256_AARCH64}"
    boot_prefix="keep_bootcon "
    ;;
  x86_64)
    kernel_sha256="${SMOKE_KERNEL_SHA256_X86_64}"
    initramfs_sha256="${SMOKE_INITRAMFS_SHA256_X86_64}"
    boot_prefix=""
    ;;
  *)
    echo "Unsupported Firecracker architecture: ${arch}" >&2
    exit 1
    ;;
esac

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates coreutils curl

artifact_dir="/srv/fc/artifacts"
smoke_dir="/var/lib/fc/smoke"
base_url="https://s3.amazonaws.com/spec.ccfc.min/${FIRECRACKER_CI_PREFIX}/${arch}"
kernel_name="vmlinux-${SMOKE_KERNEL_VERSION}"
initramfs_name="initramfs.cpio"

install -d -o root -g root -m 0755 "${artifact_dir}" "${smoke_dir}"

download_checked() {
  url="$1"
  destination="$2"
  expected_sha="$3"

  if [[ -f "${destination}" ]] \
    && printf '%s  %s\n' "${expected_sha}" "${destination}" | sha256sum --check --status; then
    echo "Verified existing artifact: ${destination}"
    return
  fi

  temporary_file="${destination}.download"
  rm -f "${temporary_file}"
  echo "Downloading ${url}"
  curl --fail --location --proto '=https' --tlsv1.2 \
    --output "${temporary_file}" \
    "${url}"
  printf '%s  %s\n' "${expected_sha}" "${temporary_file}" | sha256sum --check --status
  install -o root -g root -m 0644 "${temporary_file}" "${destination}"
  rm -f "${temporary_file}"
}

download_checked \
  "${base_url}/${kernel_name}" \
  "${artifact_dir}/${kernel_name}" \
  "${kernel_sha256}"

download_checked \
  "${base_url}/${initramfs_name}" \
  "${artifact_dir}/${initramfs_name}" \
  "${initramfs_sha256}"

cat > "${smoke_dir}/config.json" <<EOF
{
  "boot-source": {
    "kernel_image_path": "${artifact_dir}/${kernel_name}",
    "boot_args": "${boot_prefix}console=ttyS0 reboot=k panic=1",
    "initrd_path": "${artifact_dir}/${initramfs_name}"
  },
  "machine-config": {
    "vcpu_count": 1,
    "mem_size_mib": 256,
    "smt": false,
    "track_dirty_pages": false,
    "huge_pages": "None"
  },
  "drives": [],
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

chmod 0644 "${smoke_dir}/config.json"
echo "Prepared pinned ${arch} smoke-test artifacts and configuration"
