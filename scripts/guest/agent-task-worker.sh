#!/usr/bin/env bash
set -euo pipefail

# 这个进程运行在 microVM 内，使用实例令牌领取任务；令牌不是模型 API Key。
# 每次只执行一个任务，避免两个 Pi 进程同时修改同一工作区和会话目录。
bridge_url="${AGENT_BRIDGE_URL:?AGENT_BRIDGE_URL is required}"
bridge_token="${AGENT_BRIDGE_TOKEN:?AGENT_BRIDGE_TOKEN is required}"
mcp_url="${AGENT_MCP_URL:?AGENT_MCP_URL is required}"
mcp_token="${AGENT_MCP_TOKEN:?AGENT_MCP_TOKEN is required}"
runtime_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

post_workspace_result() {
  operation_id="$1"
  payload_file="$2"
  curl --fail --silent --show-error \
    --connect-timeout 3 --max-time 20 \
    --header "Authorization: Bearer ${bridge_token}" \
    --header 'Content-Type: application/json' \
    --data-binary "@${payload_file}" \
    "${bridge_url}/guest/workspace/${operation_id}/result" >/dev/null
}

while true; do
  # 文件请求和 Agent 任务使用同一个串行 Worker，避免两者同时改写工作区。
  workspace_file="$(mktemp /tmp/workspace-operation.XXXXXX.json)"
  workspace_status="$({
    curl --silent --show-error \
      --connect-timeout 3 --max-time 15 \
      --output "${workspace_file}" --write-out '%{http_code}' \
      --request POST \
      --header "Authorization: Bearer ${bridge_token}" \
      "${bridge_url}/guest/workspace/next"
  } || true)"
  if [[ "${workspace_status}" == "200" ]]; then
    operation_id="$(jq -r '.id // empty' "${workspace_file}")"
    result_file="$(mktemp /tmp/workspace-result.XXXXXX.json)"
    if [[ "${operation_id}" =~ ^workspace-[0-9a-f]{16}$ ]]; then
      # mktemp 默认仅 root 可读；把单次请求交给低权限 pi 用户后再执行。
      chown pi:pi "${workspace_file}"
      runuser -u pi -- env HOME=/home/pi PATH="${runtime_path}" \
        node /usr/local/sbin/workspace-operation.mjs "${workspace_file}" >"${result_file}"
      post_workspace_result "${operation_id}" "${result_file}" || true
    fi
    rm -f "${workspace_file}" "${result_file}"
    continue
  fi
  rm -f "${workspace_file}"

  task_file="$(mktemp /tmp/agent-task.XXXXXX.json)"
  http_status="$({
    curl --silent --show-error \
      --connect-timeout 3 --max-time 15 \
      --output "${task_file}" --write-out '%{http_code}' \
      --request POST \
      --header "Authorization: Bearer ${bridge_token}" \
      "${bridge_url}/guest/tasks/next"
  } || true)"

  if [[ "${http_status}" == "204" ]]; then
    rm -f "${task_file}"
    sleep 1
    continue
  fi
  if [[ "${http_status}" != "200" ]]; then
    rm -f "${task_file}"
    sleep 2
    continue
  fi

  task_id="$(jq -r '.id // empty' "${task_file}")"
  prompt="$(jq -r '.prompt // empty' "${task_file}")"
  if [[ ! "${task_id}" =~ ^task-[0-9a-f]{16}$ || -z "${prompt}" ]]; then
    rm -f "${task_file}"
    sleep 1
    continue
  fi

  # Pi JSON 模式由低权限适配器持续消费。适配器会把文本增量和工具阶段直接回传，
  # 最终 completed/failed 也由同一进程提交，避免 shell 缓冲完整答案。
  chown pi:pi "${task_file}"
  runuser -u pi -- env \
    HOME=/home/pi \
    PATH="${runtime_path}" \
    PI_SKIP_VERSION_CHECK=1 \
    AGENT_BRIDGE_URL="${bridge_url}" \
    AGENT_BRIDGE_TOKEN="${bridge_token}" \
    AGENT_MCP_URL="${mcp_url}" \
    AGENT_MCP_TOKEN="${mcp_token}" \
    node /usr/local/sbin/pi-event-forwarder.mjs "${task_file}" || true
  rm -f "${task_file}"
done
