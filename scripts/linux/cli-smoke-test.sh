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
volume_path="/srv/fc/volumes/instances/${instance_id}.ext4"
jail_path="/srv/jailer/firecracker/${instance_id}"
server_pid=""
instance_created=false
tap_name=""

cli=(
  "${repo_dir}/agentctl"
  --base-url "${base_url}"
  --state-file "${state_file}"
  --no-color
)

cleanup() {
  if [[ "${instance_created}" == true && -n "${server_pid}" ]] \
    && kill -0 "${server_pid}" 2>/dev/null; then
    "${cli[@]}" destroy "${instance_id}" --yes >/dev/null 2>&1 || true
  fi
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
  fi
  python3 "${repo_dir}/services/instance-manager/lifecycle.py" \
    --state-root "${state_root}" --database "${database_path}" \
    destroy "${instance_id}" >/dev/null 2>&1 || true
  rm -f "${volume_path}"
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

first_marker="CLI_FIRST_${suffix}"
first_output="$(
  printf '请只回复 %s，不要添加其他内容。\n/exit\n' "${first_marker}" \
    | timeout 300 "${cli[@]}" chat "${instance_id}"
)"
printf '%s\n' "${first_output}"
grep -Fq "Pi > ${first_marker}" <<< "${first_output}"

conversation_output="$("${cli[@]}" conversations "${instance_id}")"
conversation_id="$(grep -Eo 'conversation-[0-9a-f]{16}' <<< "${conversation_output}" | head -n1)"
if [[ -z "${conversation_id}" ]]; then
  echo "FAIL: agentctl did not return a conversation ID" >&2
  exit 1
fi

"${cli[@]}" stop "${instance_id}" >/dev/null
"${cli[@]}" start "${instance_id}" >/dev/null

second_output="$(
  printf '上一次要求你只回复的标记是什么？请仍然只回复该标记。\n/exit\n' \
    | timeout 300 "${cli[@]}" chat "${instance_id}" --conversation "${conversation_id}"
)"
printf '%s\n' "${second_output}"
grep -Fq "Pi > ${first_marker}" <<< "${second_output}"

history_output="$("${cli[@]}" history "${instance_id}" "${conversation_id}" --limit 20)"
grep -Fq "Pi > ${first_marker}" <<< "${history_output}"

"${cli[@]}" stop "${instance_id}" >/dev/null
"${cli[@]}" destroy "${instance_id}" --yes >/dev/null
instance_created=false
"${cli[@]}" logout >/dev/null

if [[ -e "${volume_path}" || -e "${jail_path}" ]]; then
  echo "FAIL: instance disk or jail remained after destroy" >&2
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
echo "CLI_SMOKE_READY auth=session lifecycle=create,start,stop,restart,destroy conversation=preserved cleanup=verified"
