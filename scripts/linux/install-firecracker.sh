#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"

# shellcheck disable=SC1091
. "${repo_dir}/config/versions.env"

arch="$(uname -m)"
case "${arch}" in
  aarch64)
    archive_sha256="${FIRECRACKER_TGZ_SHA256_AARCH64}"
    ;;
  x86_64)
    archive_sha256="${FIRECRACKER_TGZ_SHA256_X86_64}"
    ;;
  *)
    echo "Unsupported architecture: ${arch}" >&2
    exit 1
    ;;
esac

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer with sudo" >&2
  exit 1
fi

if command -v firecracker >/dev/null 2>&1 \
  && firecracker --version 2>&1 | grep -q "${FIRECRACKER_VERSION}" \
  && command -v jailer >/dev/null 2>&1 \
  && jailer --version 2>&1 | grep -q "${FIRECRACKER_VERSION}"; then
  echo "Firecracker ${FIRECRACKER_VERSION} is already installed"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates coreutils curl tar

work_dir="$(mktemp -d)"
trap 'rm -rf "${work_dir}"' EXIT

archive="firecracker-${FIRECRACKER_VERSION}-${arch}.tgz"
release_dir="release-${FIRECRACKER_VERSION}-${arch}"
release_url="https://github.com/firecracker-microvm/firecracker/releases/download/${FIRECRACKER_VERSION}/${archive}"

echo "Downloading ${release_url}"
curl --fail --location --proto '=https' --tlsv1.2 \
  --output "${work_dir}/${archive}" \
  "${release_url}"
printf '%s  %s\n' "${archive_sha256}" "${work_dir}/${archive}" \
  | sha256sum --check --status

tar -xzf "${work_dir}/${archive}" -C "${work_dir}"

firecracker_source="${work_dir}/${release_dir}/firecracker-${FIRECRACKER_VERSION}-${arch}"
jailer_source="${work_dir}/${release_dir}/jailer-${FIRECRACKER_VERSION}-${arch}"

test -x "${firecracker_source}"
test -x "${jailer_source}"

install -o root -g root -m 0755 "${firecracker_source}" /usr/local/bin/firecracker
install -o root -g root -m 0755 "${jailer_source}" /usr/local/bin/jailer

firecracker --version
jailer --version
echo "Firecracker installation completed"
