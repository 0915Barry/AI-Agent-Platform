#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this test with sudo" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"

# shellcheck disable=SC1091
. "${repo_dir}/config/versions.env"

config="/var/lib/fc/runtime/config.json"
test_config="/var/lib/fc/runtime/config.runtime-smoke.json"
console_log="/var/lib/fc/runtime/console.log"
api_socket="/var/lib/fc/runtime/firecracker.socket"

if [[ ! -x /usr/local/bin/firecracker ]]; then
  echo "Firecracker is not installed" >&2
  exit 1
fi
if [[ ! -f "${config}" ]]; then
  echo "Runtime configuration is missing; run build-rootfs first" >&2
  exit 1
fi

rm -f "${console_log}" "${api_socket}" "${test_config}"

# 只给验收 microVM 增加真实版本复查标志，不修改正式实例的启动参数，也不让
# 日常冷启动重新承担 Pi CLI 版本检查开销。
python3 - \
  "${config}" \
  "${test_config}" \
  "v${NODE_VERSION}" \
  "${PI_VERSION}" \
  "${AGENT_UID}" <<'PY'
import json
import sys

source, destination, expected_node, expected_pi, expected_uid = sys.argv[1:]
with open(source, encoding="utf-8") as handle:
    config = json.load(handle)
boot_args = config["boot-source"]["boot_args"]
arguments = [
    "agent_runtime_smoke=1",
    f"agent_expected_node={expected_node}",
    f"agent_expected_pi={expected_pi}",
    f"agent_expected_uid={expected_uid}",
]
tokens = boot_args.split()
for argument in arguments:
    key = argument.split("=", 1)[0]
    tokens = [token for token in tokens if not token.startswith(f"{key}=")]
    tokens.append(argument)
config["boot-source"]["boot_args"] = " ".join(tokens)
with open(destination, "w", encoding="utf-8") as handle:
    json.dump(config, handle, separators=(",", ":"))
PY

echo "Starting the Pi runtime microVM (60 second test window)..."
/usr/local/bin/firecracker \
  --api-sock "${api_socket}" \
  --config-file "${test_config}" \
  > "${console_log}" 2>&1 &
firecracker_pid=$!

cleanup() {
  if kill -0 "${firecracker_pid}" 2>/dev/null; then
    kill "${firecracker_pid}" 2>/dev/null || true
    sleep 1
    kill -9 "${firecracker_pid}" 2>/dev/null || true
  fi
  wait "${firecracker_pid}" 2>/dev/null || true
  rm -f "${api_socket}" "${test_config}"
}
trap cleanup EXIT

for _ in $(seq 1 120); do
  # 串口日志在部分终端环境中可能以 CRLF 结尾，因此只要求就绪行包含已执行
  # 验证字段，不使用严格的行尾锚点。
  if grep -q '^AGENT_RUNTIME_READY .*verification=executed' "${console_log}" 2>/dev/null; then
    echo "PASS: runtime rootfs booted with pinned Node, Pi Agent, and non-root pi user"
    grep '^AGENT_RUNTIME_READY ' "${console_log}"
    exit 0
  fi
  if ! kill -0 "${firecracker_pid}" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

echo "FAIL: runtime rootfs did not report readiness" >&2
echo "Console output:" >&2
tail -n 160 "${console_log}" >&2
exit 1
