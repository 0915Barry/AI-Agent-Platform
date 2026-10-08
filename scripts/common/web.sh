#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
web_dir="${repo_dir}/apps/web"
action="${1:-dev}"

# Linux 开发机直接复用项目固定的 Node 版本，避免不同发行版自带的 Node
# 版本导致 Vite 行为不一致。macOS 使用已安装的 Node，但仍检查最低主版本。
prepare_node() {
  if [[ "$(uname -s)" == "Linux" ]]; then
    # shellcheck disable=SC1091
    . "${repo_dir}/config/versions.env"
    case "$(uname -m)" in
      aarch64|arm64)
        node_arch="arm64"
        node_sha="${NODE_LINUX_ARM64_SHA256}"
        ;;
      x86_64|amd64)
        node_arch="x64"
        node_sha="${NODE_LINUX_X64_SHA256}"
        ;;
      *)
        echo "Unsupported Linux architecture for web development: $(uname -m)" >&2
        exit 1
        ;;
    esac

    tools_dir="${repo_dir}/.tools"
    node_dir="${tools_dir}/node-v${NODE_VERSION}-linux-${node_arch}"
    archive="${tools_dir}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"
    if [[ ! -x "${node_dir}/bin/node" ]]; then
      mkdir -p "${tools_dir}"
      if [[ -f "/srv/fc/cache/$(basename "${archive}")" ]]; then
        cp "/srv/fc/cache/$(basename "${archive}")" "${archive}"
      elif [[ ! -f "${archive}" ]]; then
        echo "Downloading pinned Node.js ${NODE_VERSION} for the web console..."
        curl --fail --location --proto '=https' --tlsv1.2 \
          --retry 5 --retry-all-errors --retry-delay 2 \
          --output "${archive}.download" \
          "https://nodejs.org/dist/v${NODE_VERSION}/$(basename "${archive}")"
        mv "${archive}.download" "${archive}"
      fi
      printf '%s  %s\n' "${node_sha}" "${archive}" | sha256sum --check --status
      tar -xJf "${archive}" -C "${tools_dir}"
    fi
    export PATH="${node_dir}/bin:${PATH}"
  fi

  if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    echo "Node.js and npm are required. Install Node.js 22 or newer." >&2
    exit 1
  fi
  node_major="$(node -p 'Number(process.versions.node.split(".")[0])')"
  if (( node_major < 22 )); then
    echo "Node.js 22 or newer is required; found $(node --version)." >&2
    exit 1
  fi
}

install_dependencies() {
  if [[ ! -x "${web_dir}/node_modules/.bin/vite" ]]; then
    echo "Installing pinned web dependencies..."
    npm --prefix "${web_dir}" ci
  fi
}

prepare_node

case "${action}" in
  install)
    npm --prefix "${web_dir}" ci
    ;;
  build)
    install_dependencies
    npm --prefix "${web_dir}" run build
    ;;
  dev)
    install_dependencies
    exec npm --prefix "${web_dir}" run dev
    ;;
  *)
    echo "Usage: $0 <install|build|dev>" >&2
    exit 1
    ;;
esac
