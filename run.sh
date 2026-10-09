#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "${repo_dir}/.env" ]]; then
  # shellcheck disable=SC1091
  . "${repo_dir}/.env"
fi

VM_USER="${VM_USER:-agentdev}"
VM_HOST="${VM_HOST:-}"
REMOTE_DIR="${REMOTE_DIR:-/opt/ai-agent-platform}"
UTM_APP_PATH="${UTM_APP_PATH:-/Applications/UTM.app}"
export UTM_APP_PATH

usage() {
  cat <<'EOF'
Usage: ./run.sh <command>

Commands:
  bootstrap [vm-ip] [vm-user]
                         One-command install/build after the Ubuntu VM exists
  verify                 Run the current M18, M16, and workspace checks
  start                  Start the long-running loopback control plane
  stop                   Stop the control plane; preserve instance data
  status                 Show whether the control plane is running
  tunnel                Forward local port 18090 to the VM control plane
  web-install           Install the pinned M12 web dependencies
  web-build             Type-check and build the M12 web console
  web-dev               Start the loopback-only M12 web console
  doctor                Check the macOS host
  configure <vm-ip> [vm-user]
                         Save the Linux VM connection settings
  sync                  Copy the repository into the Linux VM
  vm-doctor             Check Linux, architecture, and /dev/kvm
  install-firecracker   Install the pinned Firecracker version in the VM
  prepare-microvm       Download and verify pinned minimal ARM64 boot assets
  microvm-smoke-test    Boot a minimal microVM and verify guest init
  build-rootfs          Build the pinned Ubuntu/Node/Pi runtime rootfs
  runtime-smoke-test    Boot the runtime rootfs and verify Node, Pi, and UID
  security-smoke-test   Verify guest permissions and jailer hardening
  network-smoke-test    Verify TAP, HTTPS egress, and private-network denial
  persistence-smoke-test
                         Verify read-only rootfs and persistent data volume
  gateway-smoke-test     Verify host-side credential injection and audit logs
  lifecycle-smoke-test   Verify heartbeat, idle reaping, restart, and destroy
  configure-deepseek     Securely save a DeepSeek API key in the Linux VM
  configure-user         Create an M16 login user and claim legacy instances
  auth-smoke-test        Verify M16 sessions, ownership, and quotas
  deepseek-e2e-test      Run Pi Agent against DeepSeek through Tool Gateway
  control-plane-smoke-test
                         Control a real microVM through the M10 HTTP API
  agent-task-smoke-test  Run the managed Pi task path (legacy command alias)
  streaming-smoke-test   Verify M15 Pi JSONL, Gateway streaming, and SSE
  agent-config-smoke-test
                         Verify M17 custom prompt and tool permissions
  skills-smoke-test      Verify M18 reviewed Skill injection into Pi
  workspace-smoke-test   Verify M14 file operations and restart persistence
  control-plane-start    Start the loopback-only M10 API in the Linux VM
  control-plane-stop     Stop the M10 API; running instances remain managed
  control-plane-status   Show whether the M10 API service is running
  setup <vm-ip> [vm-user]
                         Configure, check, sync, and install Firecracker
EOF
}

bootstrap_environment() {
  if [[ -n "${1:-}" ]]; then
    configure_vm "${1}" "${2:-}"
  elif [[ -z "${VM_HOST}" ]]; then
    configure_vm
  fi

  "${repo_dir}/scripts/macos/doctor.sh"
  check_vm
  sync_repo
  echo "Bootstrapping Firecracker and the pinned Agent runtime in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" \
    "sudo '${REMOTE_DIR}/scripts/linux/install-firecracker.sh' && sudo '${REMOTE_DIR}/scripts/linux/prepare-microvm-smoke.sh' && sudo '${REMOTE_DIR}/scripts/linux/build-agent-rootfs.sh'"
  echo "BOOTSTRAP_READY host=${VM_HOST} runtime=pinned next=configure-user,configure-deepseek"
}

configure_vm() {
  vm_ip="${1:-}"
  vm_user="${2:-}"
  if [[ -n "${vm_user}" ]]; then
    VM_USER="${vm_user}"
  fi
  if [[ -z "${vm_ip}" ]]; then
    if [[ -n "${VM_HOST}" ]]; then
      read -r -p "Linux VM IP [${VM_HOST}]: " vm_ip
      vm_ip="${vm_ip:-${VM_HOST}}"
    else
      read -r -p "Linux VM IP: " vm_ip
    fi
  fi

  if [[ -z "${vm_ip}" || "${vm_ip}" =~ [[:space:]] ]]; then
    echo "A valid VM IP address or hostname is required" >&2
    exit 1
  fi

  VM_HOST="${vm_ip}"
  {
    printf 'VM_HOST=%s\n' "${VM_HOST}"
    printf 'VM_USER=%s\n' "${VM_USER}"
    printf 'REMOTE_DIR=%s\n' "${REMOTE_DIR}"
    printf 'UTM_APP_PATH=%s\n' "${UTM_APP_PATH}"
  } > "${repo_dir}/.env"

  echo "Saved VM connection settings to ${repo_dir}/.env"
}

require_vm_host() {
  if [[ -z "${VM_HOST}" ]]; then
    echo "VM_HOST is not configured. Run ./run.sh configure <vm-ip> [vm-user]." >&2
    exit 1
  fi
}

sync_repo() {
  require_vm_host
  target="${VM_USER}@${VM_HOST}"
  ssh_options=(-o ConnectTimeout=10 -o ConnectionAttempts=1 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)

  echo "Connecting to ${target} to prepare ${REMOTE_DIR}..."
  ssh "${ssh_options[@]}" -t "${target}" "sudo install -d -o '${VM_USER}' -g '${VM_USER}' '${REMOTE_DIR}'"
  echo "Synchronizing repository to ${target}:${REMOTE_DIR}..."
  rsync -az \
    -e 'ssh -o ConnectTimeout=10 -o ConnectionAttempts=1 -o ServerAliveInterval=5 -o ServerAliveCountMax=2' \
    --exclude '.git/' \
    --exclude '.env' \
    --exclude '.DS_Store' \
    --exclude '.tools/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude 'node_modules/' \
    --exclude 'dist/' \
    --exclude 'artifacts/' \
    --exclude 'runtime/' \
    "${repo_dir}/" "${target}:${REMOTE_DIR}/"
  echo "Repository synchronized to ${target}:${REMOTE_DIR}"
}

check_vm() {
  require_vm_host
  echo "Checking Linux/KVM on ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    "${VM_USER}@${VM_HOST}" 'bash -s' < "${repo_dir}/scripts/linux/doctor.sh"
}

install_firecracker() {
  sync_repo
  echo "Installing Firecracker in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" "sudo '${REMOTE_DIR}/scripts/linux/install-firecracker.sh'"
}

prepare_microvm() {
  sync_repo
  echo "Preparing pinned microVM smoke-test assets in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" "sudo '${REMOTE_DIR}/scripts/linux/prepare-microvm-smoke.sh'"
}

smoke_test_microvm() {
  prepare_microvm
  echo "Running the Firecracker microVM smoke test in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" "sudo '${REMOTE_DIR}/scripts/linux/microvm-smoke-test.sh'"
}

build_rootfs() {
  sync_repo
  echo "Building the pinned Agent runtime rootfs in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" \
    "sudo '${REMOTE_DIR}/scripts/linux/prepare-microvm-smoke.sh' && sudo '${REMOTE_DIR}/scripts/linux/build-agent-rootfs.sh'"
}

smoke_test_runtime() {
  sync_repo
  echo "Building and testing the Agent runtime rootfs in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" \
    "sudo '${REMOTE_DIR}/scripts/linux/prepare-microvm-smoke.sh' && sudo '${REMOTE_DIR}/scripts/linux/build-agent-rootfs.sh' && sudo '${REMOTE_DIR}/scripts/linux/runtime-smoke-test.sh'"
}

smoke_test_security() {
  sync_repo
  echo "Building and testing the hardened Agent runtime in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" \
    "sudo '${REMOTE_DIR}/scripts/linux/prepare-microvm-smoke.sh' && sudo '${REMOTE_DIR}/scripts/linux/build-agent-rootfs.sh' && sudo '${REMOTE_DIR}/scripts/linux/security-smoke-test.sh'"
}

smoke_test_network() {
  sync_repo
  echo "Building and testing governed microVM networking in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" \
    "sudo '${REMOTE_DIR}/scripts/linux/prepare-microvm-smoke.sh' && sudo '${REMOTE_DIR}/scripts/linux/build-agent-rootfs.sh' && sudo '${REMOTE_DIR}/scripts/linux/network-smoke-test.sh'"
}

smoke_test_persistence() {
  sync_repo
  echo "Building and testing persistent microVM storage in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" \
    "sudo '${REMOTE_DIR}/scripts/linux/prepare-microvm-smoke.sh' && sudo '${REMOTE_DIR}/scripts/linux/build-agent-rootfs.sh' && sudo '${REMOTE_DIR}/scripts/linux/persistence-smoke-test.sh'"
}

smoke_test_gateway() {
  sync_repo
  echo "Building and testing the host-side Tool Gateway in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" \
    "sudo '${REMOTE_DIR}/scripts/linux/prepare-microvm-smoke.sh' && sudo '${REMOTE_DIR}/scripts/linux/build-agent-rootfs.sh' && sudo '${REMOTE_DIR}/scripts/linux/gateway-smoke-test.sh'"
}

smoke_test_lifecycle() {
  sync_repo
  echo "Building and testing the microVM lifecycle manager in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" \
    "sudo '${REMOTE_DIR}/scripts/linux/prepare-microvm-smoke.sh' && sudo '${REMOTE_DIR}/scripts/linux/build-agent-rootfs.sh' && sudo '${REMOTE_DIR}/scripts/linux/lifecycle-smoke-test.sh'"
}

configure_deepseek() {
  require_vm_host
  sync_repo
  target="${VM_USER}@${VM_HOST}"
  read -r -s -p "DeepSeek API key (input hidden): " deepseek_api_key
  printf '\n'
  if [[ -z "${deepseek_api_key}" ]]; then
    echo "DeepSeek API key cannot be empty" >&2
    exit 1
  fi
  printf '%s\n' "${deepseek_api_key}" \
    | ssh \
      -o ConnectTimeout=10 \
      -o ConnectionAttempts=1 \
      -o ServerAliveInterval=5 \
      -o ServerAliveCountMax=2 \
      "${target}" "'${REMOTE_DIR}/scripts/linux/configure-deepseek.sh'"
  unset deepseek_api_key
}

configure_user() {
  require_vm_host
  sync_repo
  echo "Validating M16 before creating the login user in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" \
    "'${REMOTE_DIR}/scripts/linux/auth-smoke-test.sh' && '${REMOTE_DIR}/scripts/linux/configure-user.sh'"
}

smoke_test_auth() {
  sync_repo
  echo "Testing M16 authentication and instance ownership in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    "${VM_USER}@${VM_HOST}" \
    "'${REMOTE_DIR}/scripts/linux/auth-smoke-test.sh'"
}

test_deepseek_e2e() {
  sync_repo
  echo "Building and testing Pi Agent with DeepSeek in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" \
    "sudo '${REMOTE_DIR}/scripts/linux/prepare-microvm-smoke.sh' && sudo '${REMOTE_DIR}/scripts/linux/build-agent-rootfs.sh' && sudo '${REMOTE_DIR}/scripts/linux/deepseek-e2e-test.sh'"
}

smoke_test_control_plane() {
  sync_repo
  echo "Building and testing the M10 control plane in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" \
    "sudo '${REMOTE_DIR}/scripts/linux/prepare-microvm-smoke.sh' && sudo '${REMOTE_DIR}/scripts/linux/build-agent-rootfs.sh' && sudo '${REMOTE_DIR}/scripts/linux/control-plane-smoke-test.sh'"
}

smoke_test_agent_task() {
  sync_repo
  echo "Building and testing the M15 streaming Pi Agent task path in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" \
    "sudo '${REMOTE_DIR}/scripts/linux/prepare-microvm-smoke.sh' && sudo '${REMOTE_DIR}/scripts/linux/build-agent-rootfs.sh' && sudo '${REMOTE_DIR}/scripts/linux/agent-task-smoke-test.sh'"
}

smoke_test_workspace() {
  sync_repo
  echo "Building and testing the M14 persistent workspace in ${VM_USER}@${VM_HOST}..."
  ssh \
    -o ConnectTimeout=10 -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" \
    "sudo '${REMOTE_DIR}/scripts/linux/prepare-microvm-smoke.sh' && sudo '${REMOTE_DIR}/scripts/linux/build-agent-rootfs.sh' && sudo '${REMOTE_DIR}/scripts/linux/workspace-smoke-test.sh'"
}

control_plane_service() {
  action="$1"
  sync_repo
  if [[ "${action}" == "start" ]]; then
    remote_command="sudo '${REMOTE_DIR}/scripts/linux/prepare-microvm-smoke.sh' && sudo '${REMOTE_DIR}/scripts/linux/build-agent-rootfs.sh' && sudo '${REMOTE_DIR}/scripts/linux/control-plane-service.sh' start"
  else
    remote_command="sudo '${REMOTE_DIR}/scripts/linux/control-plane-service.sh' '${action}'"
  fi
  ssh \
    -o ConnectTimeout=10 \
    -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -t "${VM_USER}@${VM_HOST}" "${remote_command}"
}

control_plane_tunnel() {
  require_vm_host
  echo "Forwarding http://127.0.0.1:18090 to ${VM_USER}@${VM_HOST}. Press Ctrl-C to stop."
  exec ssh \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 \
    -N -L 127.0.0.1:18090:127.0.0.1:18090 \
    "${VM_USER}@${VM_HOST}"
}

command="${1:-}"
case "${command}" in
  bootstrap)
    bootstrap_environment "${2:-}" "${3:-}"
    ;;
  verify)
    smoke_test_auth
    smoke_test_agent_task
    smoke_test_workspace
    ;;
  start)
    control_plane_service start
    ;;
  stop)
    control_plane_service stop
    ;;
  status)
    control_plane_service status
    ;;
  tunnel)
    control_plane_tunnel
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
    "${repo_dir}/scripts/macos/doctor.sh"
    ;;
  configure)
    configure_vm "${2:-}" "${3:-}"
    ;;
  sync)
    sync_repo
    ;;
  vm-doctor)
    check_vm
    ;;
  install-firecracker)
    install_firecracker
    ;;
  prepare-microvm)
    prepare_microvm
    ;;
  microvm-smoke-test)
    smoke_test_microvm
    ;;
  build-rootfs)
    build_rootfs
    ;;
  runtime-smoke-test)
    smoke_test_runtime
    ;;
  security-smoke-test)
    smoke_test_security
    ;;
  network-smoke-test)
    smoke_test_network
    ;;
  persistence-smoke-test)
    smoke_test_persistence
    ;;
  gateway-smoke-test)
    smoke_test_gateway
    ;;
  lifecycle-smoke-test)
    smoke_test_lifecycle
    ;;
  configure-deepseek)
    configure_deepseek
    ;;
  configure-user)
    configure_user
    ;;
  auth-smoke-test)
    smoke_test_auth
    ;;
  deepseek-e2e-test)
    test_deepseek_e2e
    ;;
  control-plane-smoke-test)
    smoke_test_control_plane
    ;;
  agent-task-smoke-test)
    smoke_test_agent_task
    ;;
  streaming-smoke-test)
    smoke_test_agent_task
    ;;
  agent-config-smoke-test)
    smoke_test_agent_task
    ;;
  skills-smoke-test)
    smoke_test_agent_task
    ;;
  workspace-smoke-test)
    smoke_test_workspace
    ;;
  control-plane-start)
    control_plane_service start
    ;;
  control-plane-stop)
    control_plane_service stop
    ;;
  control-plane-status)
    control_plane_service status
    ;;
  setup)
    if [[ -n "${2:-}" ]]; then
      configure_vm "${2}" "${3:-}"
    else
      configure_vm
    fi
    "${repo_dir}/scripts/macos/doctor.sh"
    check_vm
    install_firecracker
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
