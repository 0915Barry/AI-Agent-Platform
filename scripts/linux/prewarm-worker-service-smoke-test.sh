#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"
service_script="${repo_dir}/scripts/linux/prewarm-worker-service.sh"
state_root="/var/lib/fc/prewarm-worker-service-smoke"
config_path="${state_root}/prewarm.env"
database_path="${state_root}/control-plane.db"
log_path="${state_root}/worker.log"
pid_path="/run/ai-agent-prewarm-worker-smoke.pid"
lock_path="/run/ai-agent-prewarm-worker-smoke.lock"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi
if [[ -e "${state_root}" ]]; then
  echo "Refusing to overwrite stale test state: ${state_root}" >&2
  exit 1
fi
if [[ ! -f /srv/fc/artifacts/agent-rootfs.ext4 ]]; then
  echo "Runtime rootfs is missing; run build-rootfs first" >&2
  exit 1
fi

cleanup() {
  env \
    PREWARM_CONFIG_PATH="${config_path}" \
    PREWARM_DATABASE_PATH="${database_path}" \
    PREWARM_STATE_ROOT="${state_root}" \
    PREWARM_PID_PATH="${pid_path}" \
    PREWARM_LOCK_PATH="${lock_path}" \
    PREWARM_LOG_PATH="${log_path}" \
    "${service_script}" stop >/dev/null 2>&1 || true
  rm -f "${pid_path}" "${lock_path}"
  rm -rf "${state_root}"
}
trap cleanup EXIT

install -d -o root -g root -m 0755 "${state_root}"
cat > "${config_path}" <<'EOF'
PREWARM_ENABLED=1
PREWARM_TARGET_READY=0
PREWARM_MAX_TOTAL=0
PREWARM_BUILD_CONCURRENCY=1
PREWARM_TTL_SECONDS=300
PREWARM_HEALTH_INTERVAL_SECONDS=1
PREWARM_MIN_AVAILABLE_MIB=0
PREWARM_MIN_FREE_NETWORK_SLOTS=0
EOF

service_command() {
  env \
    PREWARM_CONFIG_PATH="${config_path}" \
    PREWARM_DATABASE_PATH="${database_path}" \
    PREWARM_STATE_ROOT="${state_root}" \
    PREWARM_PID_PATH="${pid_path}" \
    PREWARM_LOCK_PATH="${lock_path}" \
    PREWARM_LOG_PATH="${log_path}" \
    "${service_script}" "$1"
}

service_command start
service_command status

cycle_ready=false
for _ in $(seq 1 50); do
  if grep -Fq 'PREWARM_WORKER_CYCLE status=ok' "${log_path}"; then
    cycle_ready=true
    break
  fi
  sleep 0.1
done
if [[ "${cycle_ready}" != true ]]; then
  echo "Worker service did not complete a cycle" >&2
  service_command logs >&2
  exit 1
fi

grep -Fq 'PREWARM_WORKER_READY' "${log_path}"
grep -Fq 'PREWARM_WORKER_RECONCILE status=ok' "${log_path}"
grep -Fq 'reason=target_satisfied' "${log_path}"

python3 -c \
  'import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); assert db.execute("SELECT COUNT(*) FROM prewarm_items").fetchone()[0] == 0; assert db.execute("SELECT COUNT(*) FROM network_leases").fetchone()[0] == 0' \
  "${database_path}"

service_command stop
if service_command status >/dev/null 2>&1; then
  echo "Worker service still reports running after stop" >&2
  exit 1
fi

echo "PASS: prewarm Worker service completed start, status, logs, and safe stop"
echo "PREWARM_WORKER_SERVICE_READY process=stopped reconcile=verified cycle=verified runtime=absent lease=absent"
