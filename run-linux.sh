#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: ./run-linux.sh <command>

Run this entry point inside the Ubuntu KVM host VM.

Commands:
  bootstrap             One-command install/build after the Ubuntu VM exists
  verify                Run the end-to-end M11 acceptance test
  start                 Start the long-running loopback control plane
  stop                  Stop the control plane; preserve instance data
  status                Show whether the control plane is running
  doctor                Check Linux, architecture, and /dev/kvm
  install-firecracker   Install the pinned Firecracker version
  prepare-microvm       Download verified artifacts for this CPU architecture
  microvm-smoke-test    Boot the minimal Firecracker microVM
  build-rootfs          Build Ubuntu, Node.js, and Pi Agent rootfs
  runtime-smoke-test    Verify Node.js, Pi Agent, and non-root UID
  security-smoke-test   Verify guest permissions and jailer hardening
  network-smoke-test    Verify governed HTTPS egress
  persistence-smoke-test
                         Verify read-only rootfs and persistent data
  gateway-smoke-test    Verify host-side credential injection
  lifecycle-smoke-test  Verify idle reaping and restart persistence
  configure-deepseek    Securely save a DeepSeek API key outside the repo
  deepseek-e2e-test     Run Pi Agent against DeepSeek through Tool Gateway
  control-plane-smoke-test
                        Control a real microVM through the M10 HTTP API
  agent-task-smoke-test Run a real M11 Pi task through the control plane
  control-plane-start   Start the loopback-only M10 HTTP API
  control-plane-stop    Stop the API; running instances remain managed
  control-plane-status  Show whether the API service is running
  setup                 Run doctor and install Firecracker
EOF
}

prepare_microvm() {
  sudo "${repo_dir}/scripts/linux/prepare-microvm-smoke.sh"
}

build_rootfs() {
  prepare_microvm
  sudo "${repo_dir}/scripts/linux/build-agent-rootfs.sh"
}

command="${1:-}"
case "${command}" in
  bootstrap)
    "${repo_dir}/scripts/linux/doctor.sh"
    sudo "${repo_dir}/scripts/linux/install-firecracker.sh"
    build_rootfs
    echo "BOOTSTRAP_READY runtime=pinned next=configure-deepseek"
    ;;
  verify)
    build_rootfs
    sudo "${repo_dir}/scripts/linux/agent-task-smoke-test.sh"
    ;;
  start)
    build_rootfs
    sudo "${repo_dir}/scripts/linux/control-plane-service.sh" start
    ;;
  stop)
    sudo "${repo_dir}/scripts/linux/control-plane-service.sh" stop
    ;;
  status)
    sudo "${repo_dir}/scripts/linux/control-plane-service.sh" status
    ;;
  doctor)
    "${repo_dir}/scripts/linux/doctor.sh"
    ;;
  install-firecracker)
    sudo "${repo_dir}/scripts/linux/install-firecracker.sh"
    ;;
  prepare-microvm)
    prepare_microvm
    ;;
  microvm-smoke-test)
    prepare_microvm
    sudo "${repo_dir}/scripts/linux/microvm-smoke-test.sh"
    ;;
  build-rootfs)
    build_rootfs
    ;;
  runtime-smoke-test)
    build_rootfs
    sudo "${repo_dir}/scripts/linux/runtime-smoke-test.sh"
    ;;
  security-smoke-test)
    build_rootfs
    sudo "${repo_dir}/scripts/linux/security-smoke-test.sh"
    ;;
  network-smoke-test)
    build_rootfs
    sudo "${repo_dir}/scripts/linux/network-smoke-test.sh"
    ;;
  persistence-smoke-test)
    build_rootfs
    sudo "${repo_dir}/scripts/linux/persistence-smoke-test.sh"
    ;;
  gateway-smoke-test)
    build_rootfs
    sudo "${repo_dir}/scripts/linux/gateway-smoke-test.sh"
    ;;
  lifecycle-smoke-test)
    build_rootfs
    sudo "${repo_dir}/scripts/linux/lifecycle-smoke-test.sh"
    ;;
  configure-deepseek)
    "${repo_dir}/scripts/linux/configure-deepseek.sh"
    ;;
  deepseek-e2e-test)
    build_rootfs
    sudo "${repo_dir}/scripts/linux/deepseek-e2e-test.sh"
    ;;
  control-plane-smoke-test)
    build_rootfs
    sudo "${repo_dir}/scripts/linux/control-plane-smoke-test.sh"
    ;;
  agent-task-smoke-test)
    build_rootfs
    sudo "${repo_dir}/scripts/linux/agent-task-smoke-test.sh"
    ;;
  control-plane-start)
    build_rootfs
    sudo "${repo_dir}/scripts/linux/control-plane-service.sh" start
    ;;
  control-plane-stop)
    sudo "${repo_dir}/scripts/linux/control-plane-service.sh" stop
    ;;
  control-plane-status)
    sudo "${repo_dir}/scripts/linux/control-plane-service.sh" status
    ;;
  setup)
    "${repo_dir}/scripts/linux/doctor.sh"
    sudo "${repo_dir}/scripts/linux/install-firecracker.sh"
    ;;
  help|-h|--help|'')
    usage
    ;;
  *)
    echo "Unknown command: ${command}" >&2
    usage >&2
    exit 1
    ;;
esac
