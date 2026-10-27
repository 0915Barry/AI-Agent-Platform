#!/usr/bin/env bash
set -euo pipefail

# socat 以非 root 的 pi 用户启动本脚本，并把当前 TCP 连接作为本进程的
# stdin/stdout。Pi RPC 因而可以直接从网络连接读取 JSONL 命令并写回事件。
if [[ "$(id -u)" -eq 0 ]]; then
  echo "Pi RPC must not run as root" >&2
  exit 1
fi

cd /workspace
export HOME=/home/pi
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PI_OFFLINE=1
export PI_SKIP_VERSION_CHECK=1
export PI_TELEMETRY=0

# 模型选择来自实例启动配置，不写死在通用系统盘中。
pi_args=(
  --offline
  --no-session
  --no-approve
  --no-extensions
  --no-skills
  --no-prompt-templates
  --no-context-files
  --tools read,write
  --mode rpc
)
if [[ -n "${PI_RPC_PROVIDER:-}" && -n "${PI_RPC_MODEL:-}" ]]; then
  pi_args+=(--provider "${PI_RPC_PROVIDER}" --model "${PI_RPC_MODEL}")
fi

# stdout 必须只包含 Pi RPC 的 JSONL 记录。诊断信息单独写入 Guest 的临时
# 文件，避免 stderr 混入 TCP 响应并破坏协议边界。
exec /usr/local/bin/pi "${pi_args[@]}" 2>>/tmp/pi-rpc.stderr
