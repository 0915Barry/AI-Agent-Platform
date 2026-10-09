#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: ./run-linux.sh <command>

Run this entry point inside the Ubuntu KVM host VM.

Commands:
  bootstrap             One-command install/build after the Ubuntu VM exists
  verify                Run the current M16, M15, and M14 checks
  start                 Start the long-running loopback control plane
  stop                  Stop the control plane; preserve instance data
  status                Show whether the control plane is running
  web-install           Install pinned Node.js and M12 web dependencies
  web-build             Type-check and build the M12 web console
  web-dev               Start the loopback-only M12 web console
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
  configure-user        Create an M16 login user and claim legacy instances
  auth-smoke-test       Verify M16 sessions, ownership, and quotas
  deepseek-e2e-test     Run Pi Agent against DeepSeek through Tool Gateway
  control-plane-smoke-test
                        Control a real microVM through the M10 HTTP API
  agent-task-smoke-test Run the managed Pi task path (legacy command alias)
  streaming-smoke-test  Verify M15 Pi JSONL, Gateway streaming, and SSE
  workspace-smoke-test  Verify M14 file operations and restart persistence
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
    echo "BOOTSTRAP_READY runtime=pinned next=configure-user,configure-deepseek"
    ;;
  verify)
    "${repo_dir}/scripts/linux/auth-smoke-test.sh"
    build_rootfs
    sudo "${repo_dir}/scripts/linux/agent-task-smoke-test.sh"
    sudo "${repo_dir}/scripts/linux/workspace-smoke-test.sh"
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
  web-install)
    "${repo_dir}/scripts/common/web.sh" install
    ;;
  web-build)
    "${repo_dir}/scripts/common/web.sh" build
    ;;
  web-dev)
    "${repo_dir}/scripts/common/web.sh" dev
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
  configure-user)
    "${repo_dir}/scripts/linux/auth-smoke-test.sh"
    "${repo_dir}/scripts/linux/configure-user.sh"
    ;;
  auth-smoke-test)
    "${repo_dir}/scripts/linux/auth-smoke-test.sh"
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
  streaming-smoke-test)
    build_rootfs
    sudo "${repo_dir}/scripts/linux/agent-task-smoke-test.sh"
    ;;
  workspace-smoke-test)
    build_rootfs
    sudo "${repo_dir}/scripts/linux/workspace-smoke-test.sh"
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
