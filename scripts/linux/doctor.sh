#!/usr/bin/env bash
set -euo pipefail

failures=0

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "FAIL: Firecracker requires a Linux host"
  exit 1
fi

arch="$(uname -m)"
echo "Linux: $(uname -r)"
echo "Architecture: ${arch}"

case "${arch}" in
  aarch64|x86_64) ;;
  *)
    echo "FAIL: unsupported Firecracker architecture: ${arch}"
    failures=$((failures + 1))
    ;;
esac

if [[ ! -c /dev/kvm ]]; then
  echo "FAIL: /dev/kvm is missing"
  failures=$((failures + 1))
elif [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then
  echo "FAIL: the current user cannot read and write /dev/kvm"
  failures=$((failures + 1))
else
  echo "KVM access: OK"
fi

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "Operating system: ${PRETTY_NAME:-unknown}"
fi

if (( failures > 0 )); then
  exit 1
fi

echo "Linux/KVM checks passed"
