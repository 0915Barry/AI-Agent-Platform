#!/usr/bin/env bash
set -euo pipefail

# socat 以非 root 的 pi 用户启动本脚本，并把当前 TCP 连接作为本进程的
# stdin/stdout。Pi RPC 因而可以直接从网络连接读取 JSONL 命令并写回事件。
if [[ "$(id -u)" -eq 0 ]]; then
  echo "Pi RPC must not run as root" >&2
  exit 1
fi

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

# Host Pi Gateway 会在首条 prompt JSONL 中附带经过数据库授权的 workspace ID。
# 本脚本只负责切换当前目录，并在交给 Pi 前移除平台扩展字段；旧客户端没有该
# 字段时继续使用 /workspace，保持协议向后兼容。
IFS= read -r first_record || exit 0
workspace_root="/workspace"
if [[ "${first_record}" =~ \"workspace\":\"(workspace-[0-9a-f]{16})\" ]]; then
  workspace_id="${BASH_REMATCH[1]}"
  workspace_root="/workspace/workspaces/${workspace_id}"
  mkdir -p "${workspace_root}"
  first_record="$(printf '%s' "${first_record}" \
    | sed -E 's/,"workspace":"workspace-[0-9a-f]{16}"}/}/')"
fi
cd "${workspace_root}"

# 先把已移除平台字段的首条记录写入管道，再持续转发 TCP
# 中的后续 JSONL。这里不能使用 Bash 进程替换 `< <(...)`，因为精简
# Guest 的 /dev 中没有 /dev/fd；普通管道不依赖该虚拟路径。
# Pi stdout 仍直接连到 socat/TCP，stderr 则沿串口进入 Host console.log。
set +e
{
  printf '%s\n' "${first_record}"
  cat
} | /usr/local/bin/pi "${pi_args[@]}"
pipeline_status=("${PIPESTATUS[@]}")
set -e
exit "${pipeline_status[1]}"
