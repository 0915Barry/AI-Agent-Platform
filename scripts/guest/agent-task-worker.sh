#!/usr/bin/env bash
set -euo pipefail

# 这个进程运行在 microVM 内，使用实例令牌领取任务；令牌不是模型 API Key。
# 每次只执行一个任务，避免两个 Pi 进程同时修改同一工作区和会话目录。
bridge_url="${AGENT_BRIDGE_URL:?AGENT_BRIDGE_URL is required}"
bridge_token="${AGENT_BRIDGE_TOKEN:?AGENT_BRIDGE_TOKEN is required}"
runtime_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

post_event() {
  task_id="$1"
  payload_file="$2"
  curl --fail --silent --show-error \
    --connect-timeout 3 --max-time 15 \
    --header "Authorization: Bearer ${bridge_token}" \
    --header 'Content-Type: application/json' \
    --data-binary "@${payload_file}" \
    "${bridge_url}/guest/tasks/${task_id}/events" >/dev/null
}

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
  rm -f "${task_file}"
  if [[ ! "${task_id}" =~ ^task-[0-9a-f]{16}$ || -z "${prompt}" ]]; then
    sleep 1
    continue
  fi

  response_file="$(mktemp /tmp/agent-response.XXXXXX.txt)"
  error_file="$(mktemp /tmp/agent-error.XXXXXX.txt)"
  event_file="$(mktemp /tmp/agent-event.XXXXXX.json)"

  if (
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
        --tools read,write \
        --provider deepseek-gateway \
        --model deepseek-flash \
        --print \
        "${prompt}"
  ) >"${response_file}" 2>"${error_file}"; then
    jq -n --rawfile output "${response_file}" \
      '{type:"completed",output:$output}' > "${event_file}"
  else
    jq -n --rawfile error "${error_file}" \
      '{type:"failed",error:($error | .[0:4000])}' > "${event_file}"
  fi

  post_event "${task_id}" "${event_file}" || true
  rm -f "${response_file}" "${error_file}" "${event_file}"
done
