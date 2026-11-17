#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"
worker_path="${repo_dir}/services/instance-manager/prewarm_worker_main.py"
config_path="${repo_dir}/config/prewarm.env"
database_path="/var/lib/fc/control-plane.db"
state_root="/var/lib/fc"
pid_path="/run/ai-agent-prewarm-worker.pid"
lock_path="/run/ai-agent-prewarm-worker.lock"
log_path="/var/log/ai-agent-prewarm-worker.log"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this command with sudo" >&2
  exit 1
fi

read_pid() {
  [[ -f "${pid_path}" ]] || return 1
  local process_pid
  process_pid="$(<"${pid_path}")"
  [[ "${process_pid}" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "${process_pid}"
}

is_worker_process() {
  local process_pid="$1"
  [[ -r "/proc/${process_pid}/cmdline" ]] \
    && tr '\0' ' ' < "/proc/${process_pid}/cmdline" \
      | grep -Fq -- "${worker_path}"
}

find_worker_pid() {
  local process_pid=""
  if process_pid="$(read_pid 2>/dev/null)" \
    && kill -0 "${process_pid}" 2>/dev/null \
    && is_worker_process "${process_pid}"; then
    printf '%s\n' "${process_pid}"
    return 0
  fi
  local process_dir
  for process_dir in /proc/[0-9]*; do
    process_pid="${process_dir##*/}"
    if is_worker_process "${process_pid}"; then
      printf '%s\n' "${process_pid}"
      return 0
    fi
  done
  return 1
}

config_is_enabled() {
  python3 "${repo_dir}/services/instance-manager/prewarm_config.py" \
    | grep -Fq 'enabled=1'
}

start_service() {
  local existing_pid=""
  if existing_pid="$(find_worker_pid 2>/dev/null)"; then
    printf '%s\n' "${existing_pid}" > "${pid_path}"
    echo "Prewarm Worker is already running with PID ${existing_pid}"
    return
  fi
  if ! config_is_enabled; then
    echo "Prewarm Worker is disabled in ${config_path}" >&2
    echo "Set PREWARM_ENABLED=1 only when the pool is ready to run." >&2
    exit 1
  fi
  if [[ ! -f /srv/fc/artifacts/agent-rootfs.ext4 ]]; then
    echo "Runtime rootfs is missing; run build-rootfs first" >&2
    exit 1
  fi

  rm -f "${pid_path}"
  install -d -o root -g root -m 0755 "${state_root}"
  touch "${log_path}"
  chmod 0640 "${log_path}"
  local log_start
  log_start="$(stat -c %s "${log_path}")"

  nohup python3 "${worker_path}" \
    --database "${database_path}" \
    --state-root "${state_root}" \
    --prewarm-config "${config_path}" \
    --lock-file "${lock_path}" \
    >> "${log_path}" 2>&1 &
  local process_pid=$!
  printf '%s\n' "${process_pid}" > "${pid_path}"

  local ready=false
  for _ in $(seq 1 50); do
    if tail -c "+$((log_start + 1))" "${log_path}" \
      | grep -Fq 'PREWARM_WORKER_READY'; then
      ready=true
      break
    fi
    if ! kill -0 "${process_pid}" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
  if [[ "${ready}" != true ]]; then
    echo "Prewarm Worker failed to start" >&2
    tail -n 100 "${log_path}" >&2
    rm -f "${pid_path}"
    exit 1
  fi
  echo "PREWARM_WORKER_SERVICE_STARTED pid=${process_pid} log=${log_path}"
}

stop_service() {
  local process_pid=""
  if ! process_pid="$(find_worker_pid 2>/dev/null)"; then
    rm -f "${pid_path}"
    echo "Prewarm Worker is not running"
    return
  fi
  if ! is_worker_process "${process_pid}"; then
    echo "Refusing to stop PID ${process_pid}: process identity does not match" >&2
    exit 1
  fi

  kill "${process_pid}"
  # Worker 可能正在等待 WARM_READY；SIGTERM 只请求安全退出，不在资源操作中途
  # 强杀。最长等待略高于单台预热机的 60 秒准备超时。
  for _ in $(seq 1 375); do
    if ! kill -0 "${process_pid}" 2>/dev/null; then
      rm -f "${pid_path}"
      echo "PREWARM_WORKER_SERVICE_STOPPED pid=${process_pid} pool=preserved"
      return
    fi
    sleep 0.2
  done
  echo "Prewarm Worker did not stop within 75 seconds; refusing to force-kill it" >&2
  exit 1
}

status_service() {
  local process_pid=""
  if process_pid="$(find_worker_pid 2>/dev/null)"; then
    printf '%s\n' "${process_pid}" > "${pid_path}"
    echo "PREWARM_WORKER_SERVICE_STATUS status=running pid=${process_pid}"
    return
  fi
  echo "PREWARM_WORKER_SERVICE_STATUS status=stopped"
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
