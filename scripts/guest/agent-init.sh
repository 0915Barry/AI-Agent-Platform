#!/usr/bin/env bash
set -euo pipefail

mount_if_needed() {
  target="$1"
  filesystem="$2"
  source="$3"

  mkdir -p "${target}"
  if ! mountpoint -q "${target}"; then
    mount -t "${filesystem}" "${source}" "${target}"
  fi
}

mount_if_needed /proc proc proc
mount_if_needed /sys sysfs sysfs
mount_if_needed /dev devtmpfs devtmpfs
mount_if_needed /dev/pts devpts devpts

exec > /dev/ttyS0 2>&1

# GUEST_STARTUP_TIMING_DIAGNOSTIC_BEGIN
# 临时 Guest 启动性能诊断。完成优化后可按
# GUEST_STARTUP_TIMING_DIAGNOSTIC 全局搜索并删除这一组函数和调用。
guest_uptime_ms() {
  local uptime_seconds whole fraction
  read -r uptime_seconds _ < /proc/uptime
  whole="${uptime_seconds%%.*}"
  fraction="${uptime_seconds#*.}000"
  fraction="${fraction:0:3}"
  printf '%d\n' "$((10#${whole} * 1000 + 10#${fraction}))"
}

guest_timing_previous_ms=0
guest_timing_mark() {
  local stage="$1" current_ms delta_ms
  current_ms="$(guest_uptime_ms)"
  delta_ms="$((current_ms - guest_timing_previous_ms))"
  guest_timing_previous_ms="${current_ms}"
  echo "GUEST_START_TIMING stage=${stage} delta_ms=${delta_ms} uptime_ms=${current_ms}"
}

guest_timing_complete() {
  local total_ms
  total_ms="$(guest_uptime_ms)"
  echo "GUEST_START_TIMING stage=complete total_ms=${total_ms}"
}

# 包含 Guest 内核启动、根文件系统挂载以及进入 agent-init 前后的基础设备挂载。
guest_timing_mark kernel_to_init  # GUEST_STARTUP_TIMING_DIAGNOSTIC
# GUEST_STARTUP_TIMING_DIAGNOSTIC_END

cmdline_value() {
  key="$1"
  for token in $(</proc/cmdline); do
    case "${token}" in
      "${key}"=*)
        printf '%s\n' "${token#*=}"
        return 0
        ;;
    esac
  done
  return 1
}

hostname agent-microvm
runtime_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH="${runtime_path}"

data_disk="$(cmdline_value agent_data_disk || true)"
if [[ "${data_disk}" == "1" ]]; then
  mount_if_needed /run tmpfs tmpfs
  mount_if_needed /tmp tmpfs tmpfs
  mount -t ext4 /dev/vdb /mnt/agent-data
  mount --bind /mnt/agent-data/workspace /workspace
  mount --bind /mnt/agent-data/pi-state /home/pi/.pi
fi

persistence_test="$(cmdline_value agent_persistence_test || true)"
if [[ "${persistence_test}" == "1" ]]; then
  persistence_phase="$(cmdline_value agent_persistence_phase)"
  persistence_token="$(cmdline_value agent_persistence_token)"
fi

mkdir -p /workspace /home/pi/.pi/agent
chown -R pi:pi /workspace /home/pi/.pi
guest_timing_mark data_mount  # GUEST_STARTUP_TIMING_DIAGNOSTIC

network_test="$(cmdline_value agent_network_test || true)"
gateway_test="$(cmdline_value agent_gateway_test || true)"
deepseek_e2e_test="$(cmdline_value agent_deepseek_e2e_test || true)"
managed_runtime="$(cmdline_value agent_managed_runtime || true)"
if [[ "${network_test}" == "1" || "${gateway_test}" == "1" \
  || "${deepseek_e2e_test}" == "1" || "${managed_runtime}" == "1" ]]; then
  guest_ip_cidr="$(cmdline_value agent_ip)"
  guest_gateway="$(cmdline_value agent_gateway)"
  guest_dns="$(cmdline_value agent_dns)"

  ip link set dev eth0 up
  ip address replace "${guest_ip_cidr}" dev eth0
  ip route replace default via "${guest_gateway}" dev eth0
  if [[ "${data_disk}" == "1" ]]; then
    printf 'nameserver %s\noptions timeout:2 attempts:2\n' "${guest_dns}" > /run/resolv.conf
    mount --bind /run/resolv.conf /etc/resolv.conf
  else
    printf 'nameserver %s\noptions timeout:2 attempts:2\n' "${guest_dns}" > /etc/resolv.conf
  fi
fi
guest_timing_mark network  # GUEST_STARTUP_TIMING_DIAGNOSTIC

if [[ "${deepseek_e2e_test}" == "1" ]]; then
  gateway_host="$(cmdline_value agent_gateway_host)"
  gateway_port="$(cmdline_value agent_gateway_port)"
  expected_token="$(cmdline_value agent_e2e_token)"
  expected_response="DEEPSEEK_E2E_OK:${expected_token}"

  pi_response="$(
    cd /workspace
    runuser -u pi -- env \
      HOME=/home/pi \
      PATH="${runtime_path}" \
      PI_SKIP_VERSION_CHECK=1 \
      /usr/local/bin/pi \
        --offline \
        --no-session \
        --no-approve \
        --no-extensions \
        --no-skills \
        --no-prompt-templates \
        --no-context-files \
        --tools read \
        --provider deepseek-gateway \
        --model deepseek-flash \
        --print \
        "Use the read tool to read /workspace/e2e-marker.txt. Reply with exactly DEEPSEEK_E2E_OK followed by a colon and the complete file contents. Do not add any other text."
  )"

  if [[ "${pi_response}" != "${expected_response}" ]]; then
    echo "DEEPSEEK_E2E_FAILURE unexpected_response=${pi_response}" >&2
    exit 1
  fi

  if runuser -u pi -- env HOME=/home/pi PATH="${runtime_path}" \
    curl --fail --silent --connect-timeout 2 --max-time 3 \
      https://api.deepseek.com/ >/dev/null 2>&1; then
    echo "DEEPSEEK_E2E_FAILURE direct_provider_reachable" >&2
    exit 1
  fi

  echo "AGENT_DEEPSEEK_READY model=deepseek-flash tool=read response=${expected_response} credential=placeholder direct_provider=denied"
fi

if [[ "${gateway_test}" == "1" ]]; then
  gateway_host="$(cmdline_value agent_gateway_host)"
  gateway_port="$(cmdline_value agent_gateway_port)"
  upstream_port="$(cmdline_value agent_upstream_port)"
  gateway_response="$(
    runuser -u pi -- env HOME=/home/pi PATH="${runtime_path}" \
      curl --fail --silent --show-error \
        --connect-timeout 5 --max-time 15 \
        --header 'Authorization: Bearer gateway-placeholder' \
        --header 'Content-Type: application/json' \
        --data '{"model":"gateway-smoke","messages":[{"role":"user","content":"credential injection smoke test"}]}' \
        "http://${gateway_host}:${gateway_port}/v1/chat/completions"
  )"
  printf '%s' "${gateway_response}" \
    | jq -e '.credential_valid == true and .provider == "mock-upstream"' >/dev/null

  if runuser -u pi -- env HOME=/home/pi PATH="${runtime_path}" \
    curl --fail --silent --connect-timeout 2 --max-time 3 \
      "http://${gateway_host}:${upstream_port}/" >/dev/null 2>&1; then
    echo "GATEWAY_SECURITY_FAILURE direct_upstream_reachable" >&2
    exit 1
  fi

  echo "AGENT_GATEWAY_READY credential=placeholder upstream=authorized direct_upstream=denied response=gateway-smoke-response"
fi
guest_timing_mark preflight_tests  # GUEST_STARTUP_TIMING_DIAGNOSTIC

# Node、Pi 和 UID 已在 rootfs 构建阶段执行真实命令验证，并写入只读系统盘。
# 正式实例启动时只读取可信构建元数据，避免每次拉起 Node/Pi CLI 再重复验收。
build_metadata="/etc/ai-agent-platform-build"
metadata_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "${build_metadata}" | head -n 1
}
node_version="v$(metadata_value NODE_VERSION)"
pi_version="$(metadata_value PI_VERSION)"
pi_uid="$(metadata_value AGENT_UID)"
static_security_verified="$(metadata_value STATIC_SECURITY_VERIFIED)"
if [[ "${node_version}" == "v" || -z "${pi_version}" || -z "${pi_uid}" \
  || "${static_security_verified}" != "1" ]]; then
  echo "RUNTIME_METADATA_FAILURE missing_version_or_uid" >&2
  exit 1
fi
guest_timing_mark version_check  # GUEST_STARTUP_TIMING_DIAGNOSTIC

# 系统盘所有权、shadow 与 /usr/local 权限已在 rootfs 构建时验证。启动时只保留
# 与本次数据盘挂载有关的动态检查，确保 /workspace 确实可由 pi 用户写入。
runuser -u pi -- sh -c \
  'probe=/workspace/.pi-permission-probe; : > "${probe}"; rm -f "${probe}"'
guest_timing_mark security_check  # GUEST_STARTUP_TIMING_DIAGNOSTIC

echo "AGENT_RUNTIME_READY node=${node_version} pi=${pi_version} uid=${pi_uid}"
echo "AGENT_SECURITY_READY static=build-verified workspace_write=allowed"

if [[ "${managed_runtime}" == "1" ]]; then
  rpc_port="$(cmdline_value agent_rpc_port)"
  rpc_bind_ip="${guest_ip_cidr%/*}"
  rpc_provider="$(cmdline_value agent_rpc_provider || true)"
  rpc_model="$(cmdline_value agent_rpc_model || true)"

  # 每个实例一次只允许一个 TCP 客户端。连接建立后 socat 以 pi 用户启动
  # 一个长期运行的 Pi RPC 进程；连接结束后进程退出，监听器重新等待下一次
  # 连接。这样 Guest 内无需额外部署 Agent Bridge 或协议管理器。
  (
    while true; do
      if runuser -u pi -- env \
          HOME=/home/pi \
          PATH="${runtime_path}" \
          PI_OFFLINE=1 \
          PI_SKIP_VERSION_CHECK=1 \
          PI_TELEMETRY=0 \
          PI_RPC_PROVIDER="${rpc_provider}" \
          PI_RPC_MODEL="${rpc_model}" \
          socat \
            "TCP-LISTEN:${rpc_port},bind=${rpc_bind_ip},reuseaddr" \
            EXEC:/usr/local/sbin/pi-rpc-session; then
        exit_status=0
      else
        exit_status="$?"
      fi
      echo "PI_RPC_CONNECTION_CLOSED status=${exit_status}"
      # 正常会话结束后立即恢复监听，方便 Host 先读取历史再发送新消息。
      # 只有 socat 异常退出时才退避，避免故障状态下形成高频重启循环。
      if [[ "${exit_status}" -ne 0 ]]; then
        sleep 1
      fi
    done
  ) &

  rpc_ready=false
  for _ in $(seq 1 50); do
    if ss -lntH | awk -v port=":${rpc_port}" '$4 ~ port "$" { found=1 } END { exit !found }'; then
      rpc_ready=true
      break
    fi
    sleep 0.1
  done
  if [[ "${rpc_ready}" != true ]]; then
    echo "PI_RPC_FAILURE listener_not_ready" >&2
    exit 1
  fi
  # 先输出诊断计时，再输出 PI_RPC_READY。Host 看到 READY 时即可确保计时日志
  # 已经完整写入串口，不会因读取过早遗漏最后两个阶段。
  guest_timing_mark rpc_listener  # GUEST_STARTUP_TIMING_DIAGNOSTIC
  guest_timing_complete  # GUEST_STARTUP_TIMING_DIAGNOSTIC
  echo "PI_RPC_READY address=${rpc_bind_ip}:${rpc_port} transport=tcp protocol=jsonl provider=${rpc_provider:-unconfigured} model=${rpc_model:-unconfigured}"
else
  guest_timing_mark rpc_listener  # GUEST_STARTUP_TIMING_DIAGNOSTIC
  guest_timing_complete  # GUEST_STARTUP_TIMING_DIAGNOSTIC
fi

if [[ "${persistence_test}" == "1" ]]; then
  root_options="$(findmnt -n -o OPTIONS /)"
  case ",${root_options}," in
    *,ro,*) ;;
    *)
      echo "PERSISTENCE_SECURITY_FAILURE rootfs_not_readonly options=${root_options}" >&2
      exit 1
      ;;
  esac

  if touch /usr/local/.rootfs-write-probe 2>/dev/null; then
    rm -f /usr/local/.rootfs-write-probe
    echo "PERSISTENCE_SECURITY_FAILURE rootfs_write_succeeded" >&2
    exit 1
  fi

  case "${persistence_phase}" in
    write)
      runuser -u pi -- sh -c \
        'printf "%s\n" "$1" > /workspace/persistence.marker; sync' \
        sh "${persistence_token}"
      echo "AGENT_PERSISTENCE_WRITTEN token=${persistence_token} rootfs=readonly"
      ;;
    verify)
      stored_token="$(runuser -u pi -- head -n 1 /workspace/persistence.marker)"
      if [[ "${stored_token}" != "${persistence_token}" ]]; then
        echo "PERSISTENCE_SECURITY_FAILURE token_mismatch expected=${persistence_token} actual=${stored_token}" >&2
        exit 1
      fi
      echo "AGENT_PERSISTENCE_READY token=${persistence_token} rootfs=readonly data=preserved"
      ;;
    *)
      echo "PERSISTENCE_SECURITY_FAILURE invalid_phase=${persistence_phase}" >&2
      exit 1
      ;;
  esac
fi

if [[ "${network_test}" == "1" ]]; then
  ping -c 1 -W 3 "${guest_gateway}" >/dev/null
  runuser -u pi -- env HOME=/home/pi PATH="${runtime_path}" \
    getent ahostsv4 example.com >/dev/null
  runuser -u pi -- env HOME=/home/pi PATH="${runtime_path}" \
    curl --fail --silent --show-error \
      --connect-timeout 5 --max-time 15 https://example.com/ >/dev/null

  if runuser -u pi -- env HOME=/home/pi PATH="${runtime_path}" \
    curl --fail --silent --connect-timeout 2 --max-time 3 \
      http://10.255.255.1/ >/dev/null 2>&1; then
    echo "NETWORK_SECURITY_FAILURE private_network_reachable" >&2
    exit 1
  fi

  echo "AGENT_NETWORK_READY ip=${guest_ip_cidr} gateway=${guest_gateway} dns=resolved https=allowed private=denied"
  echo "Networking is enabled for this governed smoke test; model credentials are still absent."
elif [[ "${gateway_test}" == "1" || "${deepseek_e2e_test}" == "1" || "${managed_runtime}" == "1" ]]; then
  echo "Networking is restricted to the host Tool Gateway; model credentials remain outside the microVM."
else
  echo "The runtime image contains no model credentials and networking is not configured yet."
fi

trap 'poweroff -f' TERM INT
while true; do
  sleep 3600 &
  wait "$!"
done
