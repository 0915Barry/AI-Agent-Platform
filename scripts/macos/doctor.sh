#!/usr/bin/env bash
set -euo pipefail

failures=0

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "FAIL: this host check supports macOS only"
  exit 1
fi

macos_version="$(sw_vers -productVersion)"
macos_major="${macos_version%%.*}"
arch="$(uname -m)"
utm_app_path="${UTM_APP_PATH:-/Applications/UTM.app}"
utmctl="${utm_app_path}/Contents/MacOS/utmctl"
utm_info="${utm_app_path}/Contents/Info.plist"

echo "macOS: ${macos_version}"
echo "Architecture: ${arch}"

if (( macos_major < 15 )); then
  echo "FAIL: macOS 15 or newer is required"
  failures=$((failures + 1))
fi

if [[ "${arch}" != "arm64" ]]; then
  echo "FAIL: the current MVP host path requires Apple Silicon"
  failures=$((failures + 1))
fi

if [[ ! -x "${utmctl}" ]]; then
  echo "FAIL: UTM was not found at ${utm_app_path}"
  failures=$((failures + 1))
else
  utm_version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "${utm_info}")"
  echo "UTM: ${utm_version}"
fi

if (( failures > 0 )); then
  exit 1
fi

echo "Host checks passed"
