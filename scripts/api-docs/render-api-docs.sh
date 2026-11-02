#!/usr/bin/env bash
# 本地 API 文档预览服务管理（Swagger UI + Redoc）。
# 只依赖系统自带 python3、curl、lsof 与浏览器，无第三方依赖。
#
# 用法：
#   ./scripts/api-docs/render-api-docs.sh                   # 等于 start
#   ./scripts/api-docs/render-api-docs.sh start  [port]
#   ./scripts/api-docs/render-api-docs.sh stop   [port]
#   ./scripts/api-docs/render-api-docs.sh status [port]
#   ./scripts/api-docs/render-api-docs.sh restart [port]
#   默认端口 8765；也兼容旧用法 "./...render-api-docs.sh 9000"
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
serve_script="${repo_dir}/scripts/api-docs/serve-api-docs.py"
DEFAULT_PORT=8765

usage() {
  cat <<'EOF'
用法：./scripts/api-docs/render-api-docs.sh [start|stop|status|restart] [port]

  start   启动预览服务（默认端口 8765）并自动打开浏览器
  stop    停止预览服务
  status  查看预览服务是否在运行
  restart 重启预览服务
  不带子命令时默认执行 start；也兼容直接传端口（如 "./...sh 9000"）。
EOF
}

# 输出监听该端口的 PID（可能多个，换行分隔）；未监听则无输出。恒返回 0。
listening_pids() {
  lsof -ti :"$1" -sTCP:LISTEN 2>/dev/null || true
}

is_running() {
  [[ -n "$(listening_pids "$1")" ]]
}

wait_ready() {
  local port="$1" i
  for i in $(seq 1 50); do
    if curl -s -o /dev/null "http://127.0.0.1:${port}/api-preview.html"; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

open_browser() {
  local port="$1"
  local url="http://127.0.0.1:${port}/api-preview.html"
  if [[ "${NO_OPEN:-}" != "1" ]]; then
    if command -v open >/dev/null 2>&1; then
      open "$url"
    elif command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$url"
    fi
  fi
}

do_start() {
  local port="$1" log
  log="/tmp/aap-api-docs-${port}.log"
  if is_running "$port"; then
    echo "API 文档已在运行：http://127.0.0.1:${port}/api-preview.html"
    return 0
  fi
  nohup python3 "$serve_script" "$port" >> "$log" 2>&1 &
  if wait_ready "$port"; then
    echo "API 文档已启动"
    echo "  Swagger UI：http://127.0.0.1:${port}/api-preview.html"
    echo "  Redoc     ：http://127.0.0.1:${port}/api-redoc.html"
    echo "  停止      ：./scripts/api-docs/render-api-docs.sh stop ${port}"
    open_browser "$port"
  else
    echo "启动失败，日志尾部：" >&2
    tail -n 20 "$log" >&2
    return 1
  fi
}

do_stop() {
  local port="$1" pids pid i
  pids="$(listening_pids "$port")"
  if [[ -z "$pids" ]]; then
    echo "API 文档未在运行（端口 ${port}）"
    return 0
  fi
  for pid in $pids; do
    kill "$pid" 2>/dev/null || true
  done
  for i in $(seq 1 50); do
    if [[ -z "$(listening_pids "$port")" ]]; then
      echo "API 文档已停止（端口 ${port}）"
      return 0
    fi
    sleep 0.1
  done
  echo "停止超时，请手动执行：lsof -ti :${port} | xargs kill -9" >&2
  return 1
}

do_status() {
  local port="$1" pids
  pids="$(listening_pids "$port")"
  if [[ -n "$pids" ]]; then
    echo "API 文档运行中：PID $(echo "$pids" | tr '\n' ' ')，http://127.0.0.1:${port}/api-preview.html"
  else
    echo "API 文档未运行（端口 ${port}）"
    return 1
  fi
}

# 解析子命令与端口；兼容旧的「直接传端口」用法。
if [[ "${1:-}" =~ ^[0-9]+$ ]]; then
  command="start"
  port="${1}"
else
  command="${1:-start}"
  port="${2:-$DEFAULT_PORT}"
fi

if ! [[ "$port" =~ ^[0-9]+$ ]]; then
  echo "端口必须是数字：${port}" >&2
  exit 1
fi

case "$command" in
  start) do_start "$port" ;;
  stop) do_stop "$port" ;;
  status) do_status "$port" ;;
  restart)
    do_stop "$port"
    do_start "$port"
    ;;
  help|-h|--help) usage ;;
  *)
    echo "未知命令：${command}" >&2
    usage >&2
    exit 1
    ;;
esac
