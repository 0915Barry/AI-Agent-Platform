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

# Host Pi Gateway 会在首条 prompt JSONL 中附带经过数据库授权的 workspace ID
# 和平台 conversation ID。本脚本将它们转换成工作目录与 Pi --session-id，再在
# 交给 Pi 前移除这两个平台扩展字段。
IFS= read -r first_record || exit 0
workspace_root="/workspace"
if [[ "${first_record}" =~ \"workspace\":\"(workspace-[0-9a-f]{16})\" ]]; then
  workspace_id="${BASH_REMATCH[1]}"
  workspace_root="/workspace/workspaces/${workspace_id}"
  mkdir -p "${workspace_root}"
  first_record="$(printf '%s' "${first_record}" \
    | sed -E 's/,"workspace":"workspace-[0-9a-f]{16}"//')"
fi

session_id=""
if [[ "${first_record}" =~ \"session\":\"(conversation-[0-9a-f]{16})\" ]]; then
  session_id="${BASH_REMATCH[1]}"
  first_record="$(printf '%s' "${first_record}" \
    | sed -E 's/,"session":"conversation-[0-9a-f]{16}"//')"
fi
cd "${workspace_root}"

if [[ -n "${session_id}" ]]; then
  # /home/pi/.pi 已 bind mount 到持久化数据盘中的 pi-state，因此 Pi 创建的
  # JSONL Session 会在 microVM 停止和重建后继续存在。首次连接按平台 ID
  # 创建；后续连接直接打开确切文件，避免 --session-id 因工作目录推导差异
  # 找不到旧文件并误建同 ID 的空 Session。
  session_file=""
  session_file_size=-1
  shopt -s nullglob globstar
  for candidate in /home/pi/.pi/agent/sessions/**/*_"${session_id}".jsonl; do
    candidate_size="$(stat -c '%s' "${candidate}")"
    if (( candidate_size > session_file_size )); then
      session_file="${candidate}"
      session_file_size="${candidate_size}"
    fi
  done
  shopt -u nullglob globstar
  if [[ -n "${session_file}" ]]; then
    pi_args+=(--session "${session_file}")
  else
    pi_args+=(--session-id "${session_id}")
  fi
else
  # 旧任务或人工 nc 调试没有平台会话 ID 时保持临时会话，避免意外串联上下文。
  pi_args+=(--no-session)
fi

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
# Pi 的 Session 位于可写数据盘。显式刷盘后再退出，Host 看到监听器重新建立时
# 即可确认本轮 JSONL Session 已经持久化，而不是仍停留在 Guest 页缓存中。
sync
exit "${pipeline_status[1]}"
