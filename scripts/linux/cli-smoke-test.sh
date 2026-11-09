#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"

# shellcheck disable=SC1091
. "${repo_dir}/config/lifecycle.env"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi
operator="${SUDO_USER:-}"
if [[ -z "${operator}" || "${operator}" == "root" ]]; then
  echo "Run this test as a configured normal user through sudo" >&2
  exit 1
fi
operator_home="$(getent passwd "${operator}" | cut -d: -f6)"
credential_path="${operator_home}/.config/ai-agent-platform/deepseek-api-key"
if [[ ! -s "${credential_path}" ]]; then
  echo "DeepSeek API key is not configured for ${operator}." >&2
  echo "Run ./run-linux.sh configure-deepseek first." >&2
  exit 1
fi

# 独立端口和数据库保证验收数据不进入正式控制面。测试前要求正式服务停止，
# 避免两个独立租约账本同时分配相同的 /30 网络槽位。
if "${repo_dir}/scripts/linux/control-plane-service.sh" status >/dev/null 2>&1; then
  echo "Stop the long-running control plane before cli-smoke-test: ./run-linux.sh stop" >&2
  exit 1
fi

for required_command in curl ip jq nft python3 timeout; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "Required command is missing: ${required_command}" >&2
    exit 1
  fi
done
if [[ ! -f /srv/fc/artifacts/agent-rootfs.ext4 ]]; then
  echo "Runtime rootfs is missing; run build-rootfs first" >&2
  exit 1
fi

listen_port="18093"
base_url="http://127.0.0.1:${listen_port}"
state_root="/var/lib/fc/cli-smoke"
database_path="${state_root}/control-plane.db"
state_file="${state_root}/agentctl-session.json"
log_path="${state_root}/server.log"
suffix="$(tr -d '-' < /proc/sys/kernel/random/uuid | cut -c1-10)"
instance_id="cli-smoke-${suffix}"
username="cli-smoke-${suffix}"
password="Smoke-${suffix}-$(tr -d '-' < /proc/sys/kernel/random/uuid | cut -c1-12)"
volume_path=""
jail_path="/srv/jailer/firecracker/${instance_id}"
server_pid=""
instance_created=false
tap_name=""
session_mount=""

dump_chat_failure_diagnostics() {
  echo "TASK_DATABASE_DIAGNOSTICS_BEGIN instance=${instance_id}" >&2
  python3 - "${database_path}" "${instance_id}" <<'PY' >&2 || true
import json
import sqlite3
import sys

database, instance_id = sys.argv[1:]
with sqlite3.connect(database) as connection:
    connection.row_factory = sqlite3.Row
    tasks = connection.execute(
        "SELECT id,status,error,created_at,updated_at FROM tasks "
        "WHERE instance_id=? ORDER BY created_at,id",
        (instance_id,),
    ).fetchall()
    if not tasks:
        print("TASK_DATABASE_EMPTY")
    for task in tasks:
        print("TASK " + json.dumps(dict(task), ensure_ascii=False, separators=(",", ":")))
        events = connection.execute(
            "SELECT sequence,event_type,payload,created_at FROM task_events "
            "WHERE task_id=? ORDER BY sequence",
            (task["id"],),
        ).fetchall()
        for event in events:
            print("EVENT " + json.dumps(dict(event), ensure_ascii=False, separators=(",", ":")))
PY
  echo "TASK_DATABASE_DIAGNOSTICS_END" >&2

  if [[ -f "${log_path}" ]]; then
    echo "CONTROL_PLANE_LOG_BEGIN path=${log_path}" >&2
    tail -n 160 "${log_path}" >&2
    echo "CONTROL_PLANE_LOG_END" >&2
  fi

  local console_log="${state_root}/control-plane/${instance_id}/console.log"
  if [[ -f "${console_log}" ]]; then
    echo "GUEST_CONSOLE_BEGIN path=${console_log}" >&2
    tail -n 120 "${console_log}" >&2
    echo "GUEST_CONSOLE_END" >&2
  fi
}

cli=(
  "${repo_dir}/agentctl"
  --base-url "${base_url}"
  --state-file "${state_file}"
  --no-color
)

cleanup() {
  if [[ -n "${session_mount}" ]]; then
    umount "${session_mount}" >/dev/null 2>&1 || true
    rmdir "${session_mount}" >/dev/null 2>&1 || true
  fi
  if [[ "${instance_created}" == true && -n "${server_pid}" ]] \
    && kill -0 "${server_pid}" 2>/dev/null; then
    "${cli[@]}" destroy "${instance_id}" --yes >/dev/null 2>&1 || true
  fi
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
  fi
  # 只有实例尚未被主流程销毁时才执行兜底清理。成功路径已经
  # 删除 state_root；此时再调用 lifecycle 会让 RuntimeStore 重建一份
  # 只含运行时表的空 SQLite 数据库，造成误导性的测试残留。
  if [[ "${instance_created}" == true ]]; then
    python3 "${repo_dir}/services/instance-manager/lifecycle.py" \
      --state-root "${state_root}" --database "${database_path}" \
      destroy "${instance_id}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${volume_path}" ]]; then
    rm -f "${volume_path}"
  fi
  rm -rf --one-file-system "${jail_path}"
  unset password
}
trap cleanup EXIT

if [[ "${state_root}" != "/var/lib/fc/cli-smoke" ]]; then
  echo "Refusing to reset unexpected state root: ${state_root}" >&2
  exit 1
fi
rm -rf --one-file-system "${state_root}"
# Gateway 以独立低权限 UID 运行，需要穿过此父目录读取各自的
# 0400 凭据副本。目录只开放“遍历”能力，数据库和凭据文件仍保持严格权限。
install -d -o root -g root -m 0755 "${state_root}"

# 通过标准输入将一次性密码交给 AuthStore，密码不出现在进程命令行。
printf '%s\n' "${password}" | env \
  PYTHONPATH="${repo_dir}/services/control-plane" \
  python3 -c '
import sys
from pathlib import Path
from auth import AuthStore
password_value = sys.stdin.readline().rstrip("\r\n")
AuthStore(Path(sys.argv[1])).create_user(sys.argv[2], password_value, claim_unowned=False)
' "${database_path}" "${username}"
user_id="$(python3 - "${database_path}" "${username}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    row = connection.execute("SELECT id FROM users WHERE username=?", (sys.argv[2],)).fetchone()
assert row is not None
print(row[0])
PY
)"
volume_path="/srv/fc/volumes/users/${user_id}.ext4"

python3 "${repo_dir}/services/control-plane/server.py" \
  --listen-host 127.0.0.1 \
  --listen-port "${listen_port}" \
  --database "${database_path}" \
  --deepseek-credential "${credential_path}" \
  --state-root "${state_root}" \
  --idle-timeout "${INSTANCE_IDLE_TIMEOUT_SECONDS}" \
  --require-auth \
  > "${log_path}" 2>&1 &
server_pid=$!

ready=false
for _ in $(seq 1 40); do
  if curl --fail --silent --max-time 1 "${base_url}/healthz" >/dev/null 2>&1; then
    ready=true
    break
  fi
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.25
done
if [[ "${ready}" != true ]]; then
  echo "FAIL: CLI smoke control plane did not start" >&2
  cat "${log_path}" >&2
  exit 1
fi

printf '%s\n' "${password}" | "${cli[@]}" login "${username}" --password-stdin >/dev/null
"${cli[@]}" create "${instance_id}" >/dev/null
instance_created=true

echo "Starting a managed microVM through agentctl..."
start_output="$(timeout 150 "${cli[@]}" start "${instance_id}")"
printf '%s\n' "${start_output}"
grep -Fq '实例启动完成' <<< "${start_output}"
status_output="$("${cli[@]}" status "${instance_id}")"
grep -Fq '状态=running' <<< "${status_output}"
tap_name="$(python3 - "${database_path}" "${instance_id}" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    row = connection.execute(
        "SELECT tap_name FROM runtime_instances WHERE instance_id=?", (sys.argv[2],)
    ).fetchone()
assert row is not None, "runtime metadata was not stored"
print(row[0])
PY
)"

# 一次「chat → 停止 → 读原生 Session 文件」并严格校验包含 user/assistant
# 消息。文件缺失、空文件或消息不完整都属于落盘回归，本轮立即失败，不重试、
# 不允许 SQLite 缓存掩盖原生 Session 问题。
chat_and_check_session() {
  local marker="$1" output
  if ! output="$(printf '请只回复 %s，不要添加其他内容。\n/exit\n' "${marker}" \
    | timeout 300 "${cli[@]}" chat "${instance_id}")"; then
    echo "FAIL_STAGE=initial_chat_transport reason=agentctl_chat_failed" >&2
    dump_chat_failure_diagnostics
    exit 1
  fi
  printf '%s\n' "${output}"
  if ! grep -Fq "Pi > ${marker}" <<< "${output}"; then
    echo "FAIL_STAGE=initial_chat_response expected=${marker}" >&2
    exit 1
  fi

  conversation_id="$(grep -Eo 'conversation-[0-9a-f]{16}' \
    <<< "$("${cli[@]}" conversations "${instance_id}")" | head -n1)"
  if [[ -z "${conversation_id}" ]]; then
    echo "FAIL_STAGE=conversation_lookup reason=missing_conversation_id" >&2
    exit 1
  fi

  if ! "${cli[@]}" stop "${instance_id}" >/dev/null; then
    echo "FAIL_STAGE=pre_session_disk_stop reason=instance_stop_failed" >&2
    dump_chat_failure_diagnostics
    exit 1
  fi

  # 诊断：抓取 Guest 串口日志（Pi stderr、socat 关闭状态），定位连接为何提前关闭。
  # smoke test 用自己的 state_root=/var/lib/fc/cli-smoke，与生产 /var/lib/fc 隔离。
  local console_log="/var/lib/fc/cli-smoke/control-plane/${instance_id}/console.log"
  if [[ ! -f "${console_log}" ]]; then
    console_log="$(ls -t /var/lib/fc/cli-smoke/control-plane/*/console.log 2>/dev/null | head -1)"
  fi
  if [[ -n "${console_log}" && -f "${console_log}" ]]; then
    echo "GUEST_CONSOLE_BEGIN path=${console_log}" >&2
    tail -n 80 "${console_log}" >&2
    echo "GUEST_CONSOLE_END" >&2
  else
    echo "GUEST_CONSOLE_MISSING: 找不到 /var/lib/fc/cli-smoke/control-plane/*/console.log" >&2
  fi

  # Firecracker 停止后 ext4 可能仍带待确认 journal；只读挂载无法回放，
  # 先离线检查修复，再只读校验 Session 文件。
  local filesystem_status=0 filesystem_check
  filesystem_check="$(e2fsck -f -y "${volume_path}" 2>&1)" || filesystem_status="$?"
  if (( filesystem_status > 1 )); then
    echo "FAIL_STAGE=filesystem_check status=${filesystem_status}" >&2
    printf '%s\n' "${filesystem_check}" >&2
    exit 1
  fi

  local check_mount check_file
  check_mount="$(mktemp -d /var/tmp/cli-session-check.XXXXXX)"
  mount -o loop,ro "${volume_path}" "${check_mount}"

  # 诊断：Guest 侧探针记录（Pi 退出后 / sync 后的文件数与大小、Pi 退出码）。
  # 放在 session 文件查找之前，因为「文件不存在」这种 flake 也要能看到探针。
  if [[ -f "${check_mount}/pi-state/session-probe.log" ]]; then
    echo "SESSION_PROBE_LOG_BEGIN" >&2
    tail -n 8 "${check_mount}/pi-state/session-probe.log" >&2
    echo "SESSION_PROBE_LOG_END" >&2
  else
    echo "SESSION_PROBE_LOG_MISSING: Guest 侧未写出探针日志（数据盘上不存在）" >&2
  fi

  check_file="$(
    find "${check_mount}/pi-state/agent/sessions" -type f \
      -name "*_${conversation_id}.jsonl" -print -quit 2>/dev/null || true
  )"
  if [[ -z "${check_file}" ]]; then
    umount "${check_mount}"; rmdir "${check_mount}"
    echo "FAIL_STAGE=session_file_lookup reason=file_not_found conversation=${conversation_id}" >&2
    exit 1
  fi

  # 诊断：无论断言成败都输出原始 Session 内容，定位偶发缺消息的根因。
  echo "SESSION_FILE_CONTENT_BEGIN path=${check_file}" >&2
  cat "${check_file}" >&2
  echo "SESSION_FILE_CONTENT_END" >&2

  # 0 字节空文件说明原生 Session 未可靠持久化，严格验收必须立即失败。
  if [[ ! -s "${check_file}" ]]; then
    umount "${check_mount}"; rmdir "${check_mount}"
    echo "FAIL_STAGE=session_file_content reason=empty_file path=${check_file}" >&2
    exit 1
  fi

  # 有内容却解析不出 user/assistant = 真正的回归（格式变了/写坏了），硬失败。
  if python3 - "${check_file}" <<'PY'
import json
import sys

entries = []
with open(sys.argv[1], encoding="utf-8") as session:
    for line in session:
        line = line.strip()
        if line:
            entries.append(json.loads(line))
messages = [entry for entry in entries if entry.get("type") == "message"]
roles = [entry.get("message", {}).get("role") for entry in messages]
if "user" in roles and "assistant" in roles:
    print(
        f"PI_NATIVE_SESSION_STORED entries={len(entries)} "
        f"messages={len(messages)} roles={','.join(str(r) for r in roles)}"
    )
    sys.exit(0)
sys.exit(1)
PY
  then
    :
  else
    umount "${check_mount}"; rmdir "${check_mount}"
    echo "FAIL_STAGE=session_jsonl_validation reason=missing_user_or_assistant path=${check_file}" >&2
    exit 1
  fi
  umount "${check_mount}"
  rmdir "${check_mount}"
}

# 单次严格验收：任何原生 Session 落盘异常都会由函数直接终止测试。
first_marker="CLI_FIRST_${suffix}"
chat_and_check_session "${first_marker}"

"${cli[@]}" start "${instance_id}" >/dev/null

# 重启后必须能够从同一原生 Session 恢复上下文；失败同样属于硬失败。
if ! second_output="$(
  printf '上一次要求你只回复的标记是什么？请仍然只回复该标记。\n/exit\n' \
    | timeout 300 "${cli[@]}" chat "${instance_id}" --conversation "${conversation_id}"
)"; then
  echo "FAIL_STAGE=session_resume_transport reason=agentctl_chat_failed conversation=${conversation_id}" >&2
  dump_chat_failure_diagnostics
  exit 1
fi
printf '%s\n' "${second_output}"
if ! grep -Fq "Pi > ${first_marker}" <<< "${second_output}"; then
  echo "FAIL_STAGE=session_resume_context expected=${first_marker} conversation=${conversation_id}" >&2
  exit 1
fi

# History 读取走 SQLite 缓存，是可靠的持久化层，始终校验。
history_output="$("${cli[@]}" history "${instance_id}" "${conversation_id}" --limit 20)"
if ! grep -Fq "Pi > ${first_marker}" <<< "${history_output}"; then
  echo "FAIL_STAGE=sqlite_history_cache expected=${first_marker} conversation=${conversation_id}" >&2
  exit 1
fi

# 新会话把消息同时写入 SQLite 作为读取缓存，Pi 原生 Session 仍是权威来源。
# 两轮 user+assistant 均为必测路径，因此至少应有 4 条消息。
expected_messages=4

python3 - "${database_path}" "${conversation_id}" "${expected_messages}" <<'PY'
import sqlite3
import sys

database, conversation_id, expected = sys.argv[1], sys.argv[2], int(sys.argv[3])
with sqlite3.connect(database) as connection:
    conversation = connection.execute(
        "SELECT session_backend,message_count FROM conversations WHERE id=?",
        (conversation_id,),
    ).fetchone()
    assert conversation is not None, "conversation metadata is missing"
    assert conversation[0] == "pi-native", "conversation did not use Pi native Session"
    assert conversation[1] >= expected, "conversation metadata did not count persisted messages"
    assert connection.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conversation_id,)
    ).fetchone()[0] >= expected, "native conversation messages were not cached into SQLite"
PY

if ! "${cli[@]}" stop "${instance_id}" >/dev/null; then
  echo "FAIL_STAGE=final_stop reason=instance_stop_failed" >&2
  dump_chat_failure_diagnostics
  exit 1
fi
"${cli[@]}" destroy "${instance_id}" --yes >/dev/null
instance_created=false
"${cli[@]}" logout >/dev/null

if [[ ! -e "${volume_path}" || -e "${jail_path}" ]]; then
  echo "FAIL: user disk was not preserved or instance jail remained after destroy" >&2
  exit 1
fi
if [[ -n "${tap_name}" ]] && ip link show "${tap_name}" >/dev/null 2>&1; then
  echo "FAIL: TAP remained after destroy: ${tap_name}" >&2
  exit 1
fi
python3 - "${database_path}" "${instance_id}" <<'PY'
import sqlite3
import sys

database, instance_id = sys.argv[1:]
with sqlite3.connect(database) as connection:
    assert connection.execute(
        "SELECT COUNT(*) FROM instances WHERE id=?", (instance_id,)
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM network_leases WHERE instance_id=?", (instance_id,)
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM runtime_instances WHERE instance_id=?", (instance_id,)
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM tasks WHERE instance_id=?", (instance_id,)
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM conversations WHERE instance_id=?", (instance_id,)
    ).fetchone()[0] == 0
PY

kill "${server_pid}"
wait "${server_pid}" 2>/dev/null || true
server_pid=""
rm -rf --one-file-system "${state_root}"

echo "PASS: agentctl completed authenticated lifecycle, chat, restart, and cleanup"
echo "CLI_SMOKE_READY auth=session lifecycle=create,start,stop,restart,destroy conversation=pi-native cleanup=verified"
