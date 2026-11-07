#!/usr/bin/env bash
# /usr/local/sbin/prewarm-activate
# 一次性激活入口：由 agent-init 的 socat（root）在预热等待期启动。处理固定
# 单行协议，只执行预先写好的挂载、校验与启动函数，不接受任意命令或 eval。
set -euo pipefail

runtime_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH="${runtime_path}"

state_dir="/run/prewarm"
mkdir -p "${state_dir}"
activated_flag="${state_dir}/activated"
committed_flag="${state_dir}/committed"

cmdline_value() {
  local key="$1"
  local value=""
  for token in $(</proc/cmdline); do
    case "${token}" in
      "${key}"=*) value="${token#*=}"; break ;;
    esac
  done
  printf '%s\n' "${value}"
}

claim_token="$(cmdline_value agent_prewarm_token)"

reject() {
  echo "ACTIVATE_REJECTED reason=${1}"
  exit 1
}

read -r command_line || exit 0
read -r command token activation_id volume_uuid <<< "${command_line}"

if [[ -z "${claim_token}" || "${token}" != "${claim_token}" ]]; then
  reject "bad_token"
fi

case "${command}" in
  PROBE_V1)
    if [[ -f "${activated_flag}" ]]; then
      echo "PROBE_READY state=activated"
    else
      echo "PROBE_READY state=warm"
    fi
    exit 0
    ;;
  ACTIVATE_V1)
    if [[ -f "${activated_flag}" ]]; then
      reject "already_activated"
    fi
    if [[ -z "${activation_id}" || -z "${volume_uuid}" ]]; then
      reject "missing_fields"
    fi
    if mountpoint -q /mnt/agent-data; then
      reject "already_mounted"
    fi
    # 校验块设备 UUID，确认 Host 已把后端切成预期用户卷。占位盘无有效
    # 文件系统时 blkid 返回空，同样会 mismatch，阻止误挂。
    actual_uuid="$(blkid -s UUID -o value /dev/vdb 2>/dev/null || true)"
    if [[ "${actual_uuid}" != "${volume_uuid}" ]]; then
      reject "volume_uuid_mismatch"
    fi

    # 受控挂载与用户初始化（与 agent-init 冷启动分支保持同一顺序）。
    mount -t ext4 /dev/vdb /mnt/agent-data
    mount --bind /mnt/agent-data/workspace /workspace
    mount --bind /mnt/agent-data/pi-state /home/pi/.pi
    mkdir -p /workspace /home/pi/.pi/agent
    chown -R pi:pi /workspace /home/pi/.pi
    runuser -u pi -- sh -c \
      'probe=/workspace/.pi-permission-probe; : > "${probe}"; rm -f "${probe}"'

    # 启动业务 Pi RPC 监听。这里内联一份与冷启动等价的循环（不抽独立脚本，
    # 避免触碰 agent-init 的冷启动监听器）；> /dev/ttyS0 让诊断进串口，& 使
    # 监听器脱离本连接进程、激活脚本退出后仍由 PID 1 接管。
    rpc_port="$(cmdline_value agent_rpc_port)"
    agent_ip="$(cmdline_value agent_ip)"
    rpc_bind_ip="${agent_ip%/*}"
    rpc_provider="$(cmdline_value agent_rpc_provider)"
    rpc_model="$(cmdline_value agent_rpc_model)"

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
            # -t 30：与 managed_runtime 一致，避免 socat 默认 0.5 秒后杀掉刷盘中的子进程。
            socat -t 30 \
              "TCP-LISTEN:${rpc_port},bind=${rpc_bind_ip},reuseaddr" \
              EXEC:/usr/local/sbin/pi-rpc-session; then
          exit_status=0
        else
          exit_status="$?"
        fi
        echo "PI_RPC_CONNECTION_CLOSED status=${exit_status}"
        if [[ "${exit_status}" -ne 0 ]]; then
          sleep 1
        fi
      done
    ) > /dev/ttyS0 2>&1 &

    rpc_ready=false
    for _ in $(seq 1 50); do
      if ss -lntH | awk -v port=":${rpc_port}" '$4 ~ port "$" { found=1 } END { exit !found }'; then
        rpc_ready=true
        break
      fi
      sleep 0.1
    done
    if [[ "${rpc_ready}" != true ]]; then
      echo "ACTIVATE_FAILURE pi_rpc_listener_not_ready" >&2
      exit 1
    fi

    # 记录已激活状态，防重复激活；即使 Host 响应丢失后重连也不会重置。
    : > "${activated_flag}"
    echo "BOUND_READY activation_id=${activation_id} rpc_address=${rpc_bind_ip}:${rpc_port}"
    exit 0
    ;;
  COMMIT_V1)
    if [[ ! -f "${activated_flag}" ]]; then
      reject "not_activated"
    fi
    : > "${committed_flag}"
    echo "COMMITTED activation_id=${activation_id}"
    exit 0
    ;;
  *)
    reject "unknown_command"
    ;;
esac
