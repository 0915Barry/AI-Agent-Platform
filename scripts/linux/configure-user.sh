#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/../.." && pwd)"
database_path="/var/lib/fc/control-plane.db"

if [[ "${EUID}" -eq 0 ]]; then
  echo "Run this command as the normal Ubuntu user; it will request sudo when needed." >&2
  exit 1
fi

read -r -p "M16 username (lowercase, 3-32 characters): " username
username="${username,,}"
if [[ ! "${username}" =~ ^[a-z0-9][a-z0-9._-]{2,31}$ ]]; then
  echo "Invalid username" >&2
  exit 1
fi

# 先刷新 sudo 凭据，再由 Python 的 getpass 直接从 TTY 安全读取平台密码；密码不会
# 出现在命令行、环境变量、shell history 或 Git 仓库中。
sudo -v
sudo python3 "${repo_dir}/services/control-plane/auth.py" \
  --database "${database_path}" \
  create-user --username "${username}" --claim-unowned

echo "Existing instances without an owner were assigned to this user."
echo "Restart the control plane, then sign in through the web page."
