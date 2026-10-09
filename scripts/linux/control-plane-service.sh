#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"

# shellcheck disable=SC1091
. "${repo_dir}/config/lifecycle.env"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this command with sudo" >&2
  exit 1
fi

listen_host="127.0.0.1"
listen_port="18090"
pid_path="/run/ai-agent-control-plane.pid"
log_path="/var/log/ai-agent-control-plane.log"
database_path="/var/lib/fc/control-plane.db"
task_database_path="/var/lib/fc/tasks/tasks.db"
server_path="${repo_dir}/services/control-plane/server.py"

read_pid() {
  if [[ ! -f "${pid_path}" ]]; then
    return 1
  fi
  local process_pid
  process_pid="$(<"${pid_path}")"
  if [[ ! "${process_pid}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  printf '%s\n' "${process_pid}"
}

is_control_plane_process() {
  local process_pid="$1"
  [[ -r "/proc/${process_pid}/cmdline" ]] \
    && tr '\0' ' ' < "/proc/${process_pid}/cmdline" | grep -Fq -- "${server_path}"
}

find_control_plane_pid() {
  # 优先使用正常 PID 文件；若之前启动中断导致 PID 文件丢失，则扫描 /proc 找回仍在
  # 监听的控制面，避免留下无法由 stop 管理的旧版孤儿进程。
  local process_pid=""
  if process_pid="$(read_pid 2>/dev/null)" \
    && kill -0 "${process_pid}" 2>/dev/null \
    && is_control_plane_process "${process_pid}"; then
    printf '%s\n' "${process_pid}"
    return 0
  fi
  local process_dir
  for process_dir in /proc/[0-9]*; do
    process_pid="${process_dir##*/}"
    if is_control_plane_process "${process_pid}"; then
      printf '%s\n' "${process_pid}"
      return 0
    fi
  done
  return 1
}

start_service() {
  local existing_pid=""
  if existing_pid="$(find_control_plane_pid 2>/dev/null)"; then
    printf '%s\n' "${existing_pid}" > "${pid_path}"
    echo "Control plane is already running with PID ${existing_pid}"
    return
  fi

  rm -f "${pid_path}"
  local operator="${SUDO_USER:-}"
  if [[ -z "${operator}" || "${operator}" == "root" ]]; then
    echo "Start the service as a configured normal user through sudo" >&2
    exit 1
  fi
  local operator_home
  operator_home="$(getent passwd "${operator}" | cut -d: -f6)"
  local credential_path="${operator_home}/.config/ai-agent-platform/deepseek-api-key"
  if [[ ! -f "${credential_path}" ]]; then
    echo "DeepSeek API key is not configured for ${operator}." >&2
    echo "Run ./run-linux.sh configure-deepseek first." >&2
    exit 1
  fi
  install -d -o root -g root -m 0755 /var/lib/fc
  touch "${log_path}"
  chmod 0640 "${log_path}"

  nohup python3 "${server_path}" \
    --listen-host "${listen_host}" \
    --listen-port "${listen_port}" \
    --database "${database_path}" \
    --task-database "${task_database_path}" \
    --deepseek-credential "${credential_path}" \
    --state-root /var/lib/fc \
    --idle-timeout "${INSTANCE_IDLE_TIMEOUT_SECONDS}" \
    --require-auth \
    >> "${log_path}" 2>&1 &
  local process_pid=$!
  printf '%s\n' "${process_pid}" > "${pid_path}"

  local ready=false
  for _ in $(seq 1 40); do
    if python3 -c \
      'import json,urllib.request; assert json.load(urllib.request.urlopen("http://127.0.0.1:18090/healthz", timeout=1))["status"] == "ok"' \
      >/dev/null 2>&1; then
      ready=true
      break
    fi
    if ! kill -0 "${process_pid}" 2>/dev/null; then
      break
    fi
    sleep 0.25
  done
  if [[ "${ready}" != true ]]; then
    echo "Control plane failed to start" >&2
    tail -n 100 "${log_path}" >&2
    rm -f "${pid_path}"
    exit 1
  fi
  echo "CONTROL_PLANE_SERVICE_STARTED pid=${process_pid} bind=${listen_host}:${listen_port} idle_timeout=${INSTANCE_IDLE_TIMEOUT_SECONDS}s"
}

stop_service() {
  local process_pid=""
  if ! process_pid="$(find_control_plane_pid 2>/dev/null)"; then
    rm -f "${pid_path}"
    echo "Control plane is not running"
    return
  fi
  if ! kill -0 "${process_pid}" 2>/dev/null; then
    rm -f "${pid_path}"
    echo "Control plane is not running"
    return
  fi
  if ! is_control_plane_process "${process_pid}"; then
    echo "Refusing to stop PID ${process_pid}: process identity does not match" >&2
    exit 1
  fi
  kill "${process_pid}"
  for _ in $(seq 1 50); do
    if ! kill -0 "${process_pid}" 2>/dev/null; then
      rm -f "${pid_path}"
      echo "CONTROL_PLANE_SERVICE_STOPPED pid=${process_pid} instances=preserved"
      return
    fi
    sleep 0.1
  done
  echo "Control plane did not stop within five seconds" >&2
  exit 1
}

status_service() {
  local process_pid=""
  if process_pid="$(find_control_plane_pid 2>/dev/null)"; then
    printf '%s\n' "${process_pid}" > "${pid_path}"
    echo "CONTROL_PLANE_SERVICE_STATUS status=running pid=${process_pid} bind=${listen_host}:${listen_port}"
    return
  fi
  echo "CONTROL_PLANE_SERVICE_STATUS status=stopped"
  return 1
}

case "${1:-}" in
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    stop_service
    start_service
    ;;
  status)
    status_service
    ;;
  logs)
    tail -n 100 "${log_path}"
    ;;
  *)
    echo "Usage: $0 <start|stop|restart|status|logs>" >&2
    exit 1
    ;;
esac
