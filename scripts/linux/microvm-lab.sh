#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"

# shellcheck disable=SC1091
. "${repo_dir}/config/versions.env"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this command with sudo" >&2
  exit 1
fi

action="${1:-}"
instance_id="microvm-lab"
jailer_base="/srv/jailer"
jail_dir="${jailer_base}/firecracker/${instance_id}"
chroot_dir="${jail_dir}/root"
artifact_dir="/srv/fc/artifacts"
volume_dir="/srv/fc/volumes/lab"
volume_path="${volume_dir}/data.ext4"
state_dir="/var/lib/fc/lab"
pid_file="${state_dir}/firecracker.pid"
start_ticks_file="${state_dir}/firecracker.start-ticks"
console_log="${state_dir}/console.log"
kernel_source="$(find "${artifact_dir}" -maxdepth 1 -type f -name 'vmlinux-*' | sort | head -n 1)"
rootfs_source="${artifact_dir}/agent-rootfs.ext4"
lease_path="/var/lib/fc/network-leases.json"
network_helper="${repo_dir}/services/instance-manager/lab_network.py"
tap_name=""
host_ip_cidr=""
guest_ip=""
guest_ip_cidr=""
guest_gateway=""
guest_dns="1.1.1.1"
guest_mac=""
guest_subnet=""
rpc_port="8080"
nft_table=""
gateway_port="18082"
gateway_user="agent-gateway"
gateway_group="agent-gateway"
gateway_state_dir="${state_dir}/gateway"
gateway_pid_file="${state_dir}/gateway.pid"
gateway_start_ticks_file="${state_dir}/gateway.start-ticks"
gateway_credential="${gateway_state_dir}/credential"
gateway_audit="${gateway_state_dir}/audit.jsonl"
gateway_log="${gateway_state_dir}/gateway.log"

usage() {
  cat <<'EOF'
Usage: microvm-lab.sh <start|status|inspect|stop>

  start    Start one long-running two-disk laboratory microVM with Pi RPC
  status   Show its process, paths, and Firecracker drive configuration
  inspect  Show diagnostics produced from inside the Guest
  stop     Stop compute and remove the jail; preserve the data disk
EOF
}

read_pid() {
  [[ -f "${pid_file}" ]] || return 1
  local value
  value="$(<"${pid_file}")"
  [[ "${value}" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "${value}"
}

process_start_ticks() {
  local pid="$1"
  [[ -r "/proc/${pid}/stat" ]] || return 1
  awk '{print $22}' "/proc/${pid}/stat"
}

lab_is_running() {
  local pid expected_ticks actual_ticks command_line
  pid="$(read_pid)" || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  [[ -f "${start_ticks_file}" ]] || return 1
  expected_ticks="$(<"${start_ticks_file}")"
  actual_ticks="$(process_start_ticks "${pid}")" || return 1
  [[ "${actual_ticks}" == "${expected_ticks}" ]] || return 1
  command_line="$(tr '\0' ' ' < "/proc/${pid}/cmdline")"
  [[ "${command_line}" == *firecracker* ]] || return 1
}

ensure_firecracker_user() {
  if ! getent group firecracker >/dev/null; then
    groupadd --system firecracker
  fi
  if ! id firecracker >/dev/null 2>&1; then
    useradd \
      --system \
      --gid firecracker \
      --no-create-home \
      --home-dir /nonexistent \
      --shell /usr/sbin/nologin \
      firecracker
  fi
}

ensure_gateway_user() {
  if ! getent group "${gateway_group}" >/dev/null; then
    groupadd --system "${gateway_group}"
  fi
  if ! id "${gateway_user}" >/dev/null 2>&1; then
    useradd \
      --system \
      --gid "${gateway_group}" \
      --no-create-home \
      --home-dir /nonexistent \
      --shell /usr/sbin/nologin \
      "${gateway_user}"
  fi
}

ensure_host_dependencies() {
  local missing=false
  command -v ip >/dev/null || missing=true
  command -v nft >/dev/null || missing=true
  command -v nc >/dev/null || missing=true
  command -v python3 >/dev/null || missing=true
  command -v setpriv >/dev/null || missing=true
  if [[ "${missing}" == true ]]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends \
      iproute2 netcat-openbsd nftables python3 util-linux
  fi
}

read_gateway_pid() {
  [[ -f "${gateway_pid_file}" ]] || return 1
  local value
  value="$(<"${gateway_pid_file}")"
  [[ "${value}" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "${value}"
}

gateway_is_running() {
  local pid expected_ticks actual_ticks command_line
  pid="$(read_gateway_pid)" || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  [[ -f "${gateway_start_ticks_file}" ]] || return 1
  expected_ticks="$(<"${gateway_start_ticks_file}")"
  actual_ticks="$(process_start_ticks "${pid}")" || return 1
  [[ "${actual_ticks}" == "${expected_ticks}" ]] || return 1
  command_line="$(tr '\0' ' ' < "/proc/${pid}/cmdline")"
  [[ "${command_line}" == *services/tool-gateway/gateway.py* ]] || return 1
}

stop_gateway() {
  local pid
  if gateway_is_running; then
    pid="$(read_gateway_pid)"
    kill "${pid}" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "${pid}" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "${pid}" 2>/dev/null; then
      kill -9 "${pid}" 2>/dev/null || true
    fi
    wait "${pid}" 2>/dev/null || true
  fi
  rm -f "${gateway_pid_file}" "${gateway_start_ticks_file}" "${gateway_credential}"
}

network_command() {
  python3 "${network_helper}" "$@" --lease-path "${lease_path}"
}

load_network() {
  local fields
  fields="$(network_command "$@")" || return
  IFS=$'\t' read -r tap_name host_ip_cidr guest_ip guest_ip_cidr \
    guest_gateway guest_mac guest_subnet nft_table <<< "${fields}"
}

cleanup_network() {
  network_command release
}

prepare_network() {
  load_network prepare --uid "$(id -u firecracker)" --gateway-port "${gateway_port}"
}

configure_model_volume() {
  (
    set -e
    local mount_dir mounted
    mount_dir="$(mktemp -d /var/tmp/microvm-lab-model.XXXXXX)"
    mounted=false
    cleanup_model_mount() {
      if [[ "${mounted}" == true ]]; then
        umount "${mount_dir}" 2>/dev/null || true
      fi
      rmdir "${mount_dir}" 2>/dev/null || true
    }
    trap cleanup_model_mount EXIT

    mount -o loop "${volume_path}" "${mount_dir}"
    mounted=true
    install -d -o "${AGENT_UID}" -g "${AGENT_GID}" -m 0755 \
      "${mount_dir}/workspace" \
      "${mount_dir}/pi-state" \
      "${mount_dir}/pi-state/agent"
    cat > "${mount_dir}/pi-state/agent/models.json" <<EOF
{
  "providers": {
    "deepseek-gateway": {
      "baseUrl": "http://${guest_gateway}:${gateway_port}/v1",
      "api": "openai-completions",
      "apiKey": "gateway-placeholder",
      "models": [
        {
          "id": "deepseek-flash",
          "name": "DeepSeek Flash via host gateway",
          "reasoning": false,
          "input": ["text"],
          "contextWindow": 128000,
          "maxTokens": 8192
        }
      ]
    }
  }
}
EOF
    chown "${AGENT_UID}:${AGENT_GID}" \
      "${mount_dir}/pi-state/agent/models.json"
    chmod 0600 "${mount_dir}/pi-state/agent/models.json"
    sync
  )
  chown firecracker:firecracker "${volume_path}"
  chmod 0600 "${volume_path}"
}

start_gateway() {
  local operator operator_home credential_source gateway_uid gateway_gid gateway_pid ready

  operator="${SUDO_USER:-}"
  if [[ -z "${operator}" || "${operator}" == root ]]; then
    echo "Run lab-start as a configured normal user through sudo" >&2
    return 1
  fi
  operator_home="$(getent passwd "${operator}" | cut -d: -f6)"
  credential_source="${operator_home}/.config/ai-agent-platform/deepseek-api-key"
  if [[ ! -f "${credential_source}" ]]; then
    echo "DeepSeek API key is not configured for ${operator}." >&2
    echo "Run ./run-linux.sh configure-deepseek first." >&2
    return 1
  fi
  if [[ "$(stat -c '%U' "${credential_source}")" != "${operator}" \
    || "$(stat -c '%a' "${credential_source}")" != 600 ]]; then
    echo "DeepSeek credential must be owned by ${operator} with mode 600" >&2
    return 1
  fi

  stop_gateway
  gateway_uid="$(id -u "${gateway_user}")"
  gateway_gid="$(id -g "${gateway_group}")"
  install -d -o "${gateway_uid}" -g "${gateway_gid}" -m 0750 \
    "${gateway_state_dir}"
  install -o "${gateway_uid}" -g "${gateway_gid}" -m 0400 \
    "${credential_source}" "${gateway_credential}"
  : > "${gateway_audit}"
  : > "${gateway_log}"
  chown "${gateway_uid}:${gateway_gid}" "${gateway_audit}" "${gateway_log}"
  chmod 0600 "${gateway_audit}" "${gateway_log}"

  env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    setpriv --reuid "${gateway_uid}" --regid "${gateway_gid}" --clear-groups \
    python3 "${repo_dir}/services/tool-gateway/gateway.py" \
      --listen-host "${guest_gateway}" \
      --listen-port "${gateway_port}" \
      --upstream "https://api.deepseek.com" \
      --credential-file "${gateway_credential}" \
      --audit-file "${gateway_audit}" \
      --tenant "microvm-lab" \
      >> "${gateway_log}" 2>&1 &
  gateway_pid=$!
  printf '%s\n' "${gateway_pid}" > "${gateway_pid_file}"
  process_start_ticks "${gateway_pid}" > "${gateway_start_ticks_file}"

  ready=false
  for _ in $(seq 1 40); do
    if ss -H -ltn "sport = :${gateway_port}" \
      | grep -q "${guest_gateway}:${gateway_port}"; then
      ready=true
      break
    fi
    if ! kill -0 "${gateway_pid}" 2>/dev/null; then
      break
    fi
    sleep 0.25
  done
  if [[ "${ready}" != true ]]; then
    echo "The laboratory DeepSeek gateway did not start" >&2
    cat "${gateway_log}" >&2
    stop_gateway
    return 1
  fi
}

create_data_volume() {
  local mount_dir
  [[ -f "${volume_path}" ]] && return

  echo "Creating the persistent laboratory data disk: ${volume_path}"
  truncate -s 1G "${volume_path}"
  mkfs.ext4 -q -F -O '^orphan_file' -L agent-data "${volume_path}"
  mount_dir="$(mktemp -d /var/tmp/microvm-lab-volume.XXXXXX)"
  mount -o loop "${volume_path}" "${mount_dir}"
  install -d -o "${AGENT_UID}" -g "${AGENT_GID}" -m 0755 \
    "${mount_dir}/workspace" "${mount_dir}/pi-state"
  sync
  umount "${mount_dir}"
  rmdir "${mount_dir}"
  chown firecracker:firecracker "${volume_path}"
  chmod 0600 "${volume_path}"
}

prepare_jail() {
  local firecracker_uid firecracker_gid
  firecracker_uid="$(id -u firecracker)"
  firecracker_gid="$(id -g firecracker)"

  rm -rf --one-file-system "${jail_dir}"
  install -d -o root -g root -m 0755 "${chroot_dir}"

  ln "${kernel_source}" "${chroot_dir}/vmlinux"
  ln "${rootfs_source}" "${chroot_dir}/rootfs.ext4"
  ln "${volume_path}" "${chroot_dir}/data.ext4"

  cat > "${chroot_dir}/config.json" <<EOF
{
  "boot-source": {
    "kernel_image_path": "/vmlinux",
    "boot_args": "root=/dev/vda ro rootfstype=ext4 console=ttyS0 reboot=k panic=1 pci=off init=/usr/local/sbin/agent-init agent_data_disk=1 agent_lab=1 agent_ip=${guest_ip_cidr} agent_gateway=${guest_gateway} agent_dns=${guest_dns} agent_rpc_port=${rpc_port} agent_rpc_provider=deepseek-gateway agent_rpc_model=deepseek-flash"
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
  "network-interfaces": [
    {
      "iface_id": "eth0",
      "guest_mac": "${guest_mac}",
      "host_dev_name": "${tap_name}"
    }
  ],
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

  printf '%s\n' "${firecracker_uid}" > "${state_dir}/firecracker.uid"
  printf '%s\n' "${firecracker_gid}" > "${state_dir}/firecracker.gid"
}

start_lab() {
  local firecracker_uid firecracker_gid pid ready

  if lab_is_running; then
    echo "The laboratory microVM is already running (PID $(read_pid))"
    exit 0
  fi
  if [[ -n "$(read_pid 2>/dev/null || true)" ]]; then
    echo "Removing stale laboratory process metadata"
    rm -f "${pid_file}" "${start_ticks_file}"
  fi
  [[ -n "${kernel_source}" && -f "${kernel_source}" ]] || {
    echo "Guest kernel is missing; run prepare-microvm first" >&2
    exit 1
  }
  [[ -f "${rootfs_source}" ]] || {
    echo "Runtime rootfs is missing; run build-rootfs first" >&2
    exit 1
  }
  [[ -x /usr/local/bin/firecracker && -x /usr/local/bin/jailer ]] || {
    echo "Firecracker and jailer must be installed" >&2
    exit 1
  }

  ensure_firecracker_user
  ensure_gateway_user
  ensure_host_dependencies
  install -d -o root -g root -m 0755 \
    "${jailer_base}/firecracker" "${volume_dir}" "${state_dir}"
  create_data_volume
  # 从这里开始，任何启动失败都停止进程并释放网络；数据盘始终保留。
  trap 'result=$?; if [[ "${result}" -ne 0 ]]; then stop_lab; fi' EXIT
  prepare_network
  configure_model_volume
  if ! start_gateway; then
    cleanup_network
    exit 1
  fi
  prepare_jail

  firecracker_uid="$(<"${state_dir}/firecracker.uid")"
  firecracker_gid="$(<"${state_dir}/firecracker.gid")"
  : > "${console_log}"
  chmod 0640 "${console_log}"

  echo "Starting the laboratory microVM..."
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
      >> "${console_log}" 2>&1 &
  pid=$!
  printf '%s\n' "${pid}" > "${pid_file}"
  process_start_ticks "${pid}" > "${start_ticks_file}"

  ready=false
  for _ in $(seq 1 120); do
    if grep -q '^PI_RPC_READY ' "${console_log}" 2>/dev/null; then
      ready=true
      break
    fi
    if ! lab_is_running; then
      break
    fi
    sleep 0.5
  done

  if [[ "${ready}" != true ]]; then
    echo "The laboratory microVM did not become ready" >&2
    tail -n 160 "${console_log}" >&2
    stop_lab || true
    exit 1
  fi

  network_command activate
  trap - EXIT

  echo "Network leases: ${lease_path}"
  echo "MICROVM_LAB_STARTED pid=${pid} rootfs=readonly data=preserved network=host-only"
  echo "PI_RPC_ENDPOINT ${guest_ip}:${rpc_port}"
  echo "MODEL_GATEWAY_ENDPOINT ${guest_gateway}:${gateway_port} provider=deepseek-gateway model=deepseek-flash credential=host-only"
  echo "From the Ubuntu host: nc ${guest_ip} ${rpc_port}"
  echo 'Then send one JSONL line: {"id":"demo-1","type":"get_state"}'
}

status_lab() {
  if ! lab_is_running; then
    echo "MICROVM_LAB_STATUS state=stopped"
    echo "Persistent data disk: ${volume_path}"
    exit 0
  fi

  load_network get
  echo "Network leases: ${lease_path}"
  echo "MICROVM_LAB_STATUS state=running pid=$(read_pid)"
  echo "Jail: ${jail_dir}"
  echo "Configuration: ${chroot_dir}/config.json"
  echo "Console log: ${console_log}"
  echo "Persistent data disk: ${volume_path}"
  echo "Pi RPC endpoint: ${guest_ip}:${rpc_port} (reachable from this Ubuntu host)"
  if gateway_is_running; then
    echo "Model gateway: running pid=$(read_gateway_pid) bind=${guest_gateway}:${gateway_port}"
  else
    echo "Model gateway: stopped"
  fi
  echo "Configured drives:"
  sed -n '/"drives"[[:space:]]*:/,/^[[:space:]]*],/p' "${chroot_dir}/config.json"
}

inspect_lab() {
  [[ -f "${console_log}" ]] || {
    echo "No laboratory console log exists; run lab-start first" >&2
    exit 1
  }
  if ! grep -q '^MICROVM_LAB_READY ' "${console_log}"; then
    echo "Guest diagnostics are not available yet" >&2
    tail -n 80 "${console_log}" >&2
    exit 1
  fi
  sed -n '/^MICROVM_LAB_READY /,$p' "${console_log}"
}

stop_lab() {
  local pid
  if lab_is_running; then
    pid="$(read_pid)"
    kill "${pid}"
    for _ in $(seq 1 20); do
      kill -0 "${pid}" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "${pid}" 2>/dev/null; then
      kill -9 "${pid}"
    fi
    wait "${pid}" 2>/dev/null || true
  fi
  rm -f "${pid_file}" "${start_ticks_file}"
  rm -rf --one-file-system "${jail_dir}"
  stop_gateway
  cleanup_network
  echo "MICROVM_LAB_STOPPED data=preserved path=${volume_path}"
}

case "${action}" in
  start) start_lab ;;
  status) status_lab ;;
  inspect) inspect_lab ;;
  stop) stop_lab ;;
  help|-h|--help|'') usage ;;
  *)
    echo "Unknown action: ${action}" >&2
    usage >&2
    exit 1
    ;;
esac
