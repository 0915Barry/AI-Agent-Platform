#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -eq 0 ]]; then
  echo "Run this command as the normal Linux user, not with sudo" >&2
  exit 1
fi

config_dir="${HOME}/.config/ai-agent-platform"
credential_path="${config_dir}/deepseek-api-key"

if [[ -t 0 ]]; then
  read -r -s -p "DeepSeek API key: " api_key
  printf '\n'
else
  IFS= read -r api_key
fi

api_key="${api_key%$'\r'}"
if [[ -z "${api_key}" || "${#api_key}" -lt 12 ]]; then
  echo "DeepSeek API key is empty or unexpectedly short" >&2
  unset api_key
  exit 1
fi
if [[ "${api_key}" == *[[:space:]]* ]]; then
  echo "DeepSeek API key must not contain whitespace" >&2
  unset api_key
  exit 1
fi

umask 077
install -d -m 0700 "${config_dir}"
temporary_path="$(mktemp "${config_dir}/.deepseek-api-key.XXXXXX")"
cleanup() {
  rm -f "${temporary_path}"
  unset api_key
}
trap cleanup EXIT

printf '%s\n' "${api_key}" > "${temporary_path}"
chmod 0600 "${temporary_path}"
mv -f "${temporary_path}" "${credential_path}"
chmod 0600 "${credential_path}"
unset api_key

echo "Saved the DeepSeek credential outside the repository: ${credential_path}"
echo "Credential permissions: $(stat -c '%a' "${credential_path}")"
