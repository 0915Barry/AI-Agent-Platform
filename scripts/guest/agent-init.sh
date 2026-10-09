#!/usr/bin/env bash
set -euo pipefail

mount_if_needed() {
  target="$1"
  filesystem="$2"
  source="$3"

  mkdir -p "${target}"
  if ! mountpoint -q "${target}"; then
    mount -t "${filesystem}" "${source}" "${target}"
  fi
}

mount_if_needed /proc proc proc
mount_if_needed /sys sysfs sysfs
mount_if_needed /dev devtmpfs devtmpfs
mount_if_needed /dev/pts devpts devpts

exec > /dev/ttyS0 2>&1

cmdline_value() {
  key="$1"
  for token in $(</proc/cmdline); do
    case "${token}" in
      "${key}"=*)
        printf '%s\n' "${token#*=}"
        return 0
        ;;
    esac
  done
  return 1
}

hostname agent-microvm
runtime_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH="${runtime_path}"

data_disk="$(cmdline_value agent_data_disk || true)"
if [[ "${data_disk}" == "1" ]]; then
  mount_if_needed /run tmpfs tmpfs
  mount_if_needed /tmp tmpfs tmpfs
  mount -t ext4 /dev/vdb /mnt/agent-data
  mount --bind /mnt/agent-data/workspace /workspace
  mount --bind /mnt/agent-data/pi-state /home/pi/.pi
fi

persistence_test="$(cmdline_value agent_persistence_test || true)"
if [[ "${persistence_test}" == "1" ]]; then
  persistence_phase="$(cmdline_value agent_persistence_phase)"
  persistence_token="$(cmdline_value agent_persistence_token)"
fi

mkdir -p /workspace /home/pi/.pi/agent
chown -R pi:pi /workspace /home/pi/.pi

network_test="$(cmdline_value agent_network_test || true)"
gateway_test="$(cmdline_value agent_gateway_test || true)"
deepseek_e2e_test="$(cmdline_value agent_deepseek_e2e_test || true)"
managed_runtime="$(cmdline_value agent_managed_runtime || true)"
if [[ "${network_test}" == "1" || "${gateway_test}" == "1" \
  || "${deepseek_e2e_test}" == "1" || "${managed_runtime}" == "1" ]]; then
  guest_ip_cidr="$(cmdline_value agent_ip)"
  guest_gateway="$(cmdline_value agent_gateway)"
  guest_dns="$(cmdline_value agent_dns)"

  ip link set dev eth0 up
  ip address replace "${guest_ip_cidr}" dev eth0
  ip route replace default via "${guest_gateway}" dev eth0
  if [[ "${data_disk}" == "1" ]]; then
    printf 'nameserver %s\noptions timeout:2 attempts:2\n' "${guest_dns}" > /run/resolv.conf
    mount --bind /run/resolv.conf /etc/resolv.conf
  else
    printf 'nameserver %s\noptions timeout:2 attempts:2\n' "${guest_dns}" > /etc/resolv.conf
  fi
fi

if [[ "${deepseek_e2e_test}" == "1" ]]; then
  gateway_host="$(cmdline_value agent_gateway_host)"
  gateway_port="$(cmdline_value agent_gateway_port)"
  expected_token="$(cmdline_value agent_e2e_token)"
  expected_response="DEEPSEEK_E2E_OK:${expected_token}"

  pi_response="$(
    cd /workspace
    runuser -u pi -- env \
      HOME=/home/pi \
      PATH="${runtime_path}" \
      PI_SKIP_VERSION_CHECK=1 \
      /usr/local/bin/pi \
        --offline \
        --no-session \
        --no-approve \
        --no-extensions \
        --no-skills \
        --no-prompt-templates \
        --no-context-files \
        --tools read \
        --provider deepseek-gateway \
        --model deepseek-flash \
        --print \
        "Use the read tool to read /workspace/e2e-marker.txt. Reply with exactly DEEPSEEK_E2E_OK followed by a colon and the complete file contents. Do not add any other text."
  )"

  if [[ "${pi_response}" != "${expected_response}" ]]; then
    echo "DEEPSEEK_E2E_FAILURE unexpected_response=${pi_response}" >&2
    exit 1
  fi

  if runuser -u pi -- env HOME=/home/pi PATH="${runtime_path}" \
    curl --fail --silent --connect-timeout 2 --max-time 3 \
      https://api.deepseek.com/ >/dev/null 2>&1; then
    echo "DEEPSEEK_E2E_FAILURE direct_provider_reachable" >&2
    exit 1
  fi

  echo "AGENT_DEEPSEEK_READY model=deepseek-flash tool=read response=${expected_response} credential=placeholder direct_provider=denied"
fi

if [[ "${gateway_test}" == "1" ]]; then
  gateway_host="$(cmdline_value agent_gateway_host)"
  gateway_port="$(cmdline_value agent_gateway_port)"
  upstream_port="$(cmdline_value agent_upstream_port)"
  gateway_response="$(
    runuser -u pi -- env HOME=/home/pi PATH="${runtime_path}" \
      curl --fail --silent --show-error \
        --connect-timeout 5 --max-time 15 \
        --header 'Authorization: Bearer gateway-placeholder' \
        --header 'Content-Type: application/json' \
        --data '{"model":"gateway-smoke","messages":[{"role":"user","content":"credential injection smoke test"}]}' \
        "http://${gateway_host}:${gateway_port}/v1/chat/completions"
  )"
  printf '%s' "${gateway_response}" \
    | jq -e '.credential_valid == true and .provider == "mock-upstream"' >/dev/null

  if runuser -u pi -- env HOME=/home/pi PATH="${runtime_path}" \
    curl --fail --silent --connect-timeout 2 --max-time 3 \
      "http://${gateway_host}:${upstream_port}/" >/dev/null 2>&1; then
    echo "GATEWAY_SECURITY_FAILURE direct_upstream_reachable" >&2
    exit 1
  fi

  echo "AGENT_GATEWAY_READY credential=placeholder upstream=authorized direct_upstream=denied response=gateway-smoke-response"
fi

node_version="$(
  runuser -u pi -- env \
    HOME=/home/pi \
    PATH="${runtime_path}" \
    /usr/local/bin/node --version
)"
pi_version="$(
  runuser -u pi -- env \
    HOME=/home/pi \
    PATH="${runtime_path}" \
    PI_OFFLINE=1 \
    PI_SKIP_VERSION_CHECK=1 \
    /usr/local/bin/pi --version
)"
pi_uid="$(id -u pi)"

node_owner="$(stat -c '%u:%g' /usr/local/bin/node)"
if [[ "${node_owner}" != "0:0" ]]; then
  echo "SECURITY_FAILURE node_owner=${node_owner}" >&2
  exit 1
fi

if runuser -u pi -- head -c 1 /etc/shadow >/dev/null 2>&1; then
  echo "SECURITY_FAILURE pi_can_read_shadow" >&2
  exit 1
fi

if runuser -u pi -- sh -c ': > /usr/local/.pi-permission-probe' 2>/dev/null; then
  rm -f /usr/local/.pi-permission-probe
  echo "SECURITY_FAILURE pi_can_write_runtime" >&2
  exit 1
fi

runuser -u pi -- sh -c \
  'probe=/workspace/.pi-permission-probe; : > "${probe}"; rm -f "${probe}"'

echo "AGENT_RUNTIME_READY node=${node_version} pi=${pi_version} uid=${pi_uid}"
echo "AGENT_SECURITY_READY runtime_owner=${node_owner} shadow=denied runtime_write=denied workspace_write=allowed"

if [[ "${persistence_test}" == "1" ]]; then
  root_options="$(findmnt -n -o OPTIONS /)"
  case ",${root_options}," in
    *,ro,*) ;;
    *)
      echo "PERSISTENCE_SECURITY_FAILURE rootfs_not_readonly options=${root_options}" >&2
      exit 1
      ;;
  esac

  if touch /usr/local/.rootfs-write-probe 2>/dev/null; then
    rm -f /usr/local/.rootfs-write-probe
    echo "PERSISTENCE_SECURITY_FAILURE rootfs_write_succeeded" >&2
    exit 1
  fi

  case "${persistence_phase}" in
    write)
      runuser -u pi -- sh -c \
        'printf "%s\n" "$1" > /workspace/persistence.marker; sync' \
        sh "${persistence_token}"
      echo "AGENT_PERSISTENCE_WRITTEN token=${persistence_token} rootfs=readonly"
      ;;
    verify)
      stored_token="$(runuser -u pi -- head -n 1 /workspace/persistence.marker)"
      if [[ "${stored_token}" != "${persistence_token}" ]]; then
        echo "PERSISTENCE_SECURITY_FAILURE token_mismatch expected=${persistence_token} actual=${stored_token}" >&2
        exit 1
      fi
      echo "AGENT_PERSISTENCE_READY token=${persistence_token} rootfs=readonly data=preserved"
      ;;
    *)
      echo "PERSISTENCE_SECURITY_FAILURE invalid_phase=${persistence_phase}" >&2
      exit 1
      ;;
  esac
fi

if [[ "${network_test}" == "1" ]]; then
  ping -c 1 -W 3 "${guest_gateway}" >/dev/null
  runuser -u pi -- env HOME=/home/pi PATH="${runtime_path}" \
    getent ahostsv4 example.com >/dev/null
  runuser -u pi -- env HOME=/home/pi PATH="${runtime_path}" \
    curl --fail --silent --show-error \
      --connect-timeout 5 --max-time 15 https://example.com/ >/dev/null

  if runuser -u pi -- env HOME=/home/pi PATH="${runtime_path}" \
    curl --fail --silent --connect-timeout 2 --max-time 3 \
      http://10.255.255.1/ >/dev/null 2>&1; then
    echo "NETWORK_SECURITY_FAILURE private_network_reachable" >&2
    exit 1
  fi

  echo "AGENT_NETWORK_READY ip=${guest_ip_cidr} gateway=${guest_gateway} dns=resolved https=allowed private=denied"
  echo "Networking is enabled for this governed smoke test; model credentials are still absent."
elif [[ "${gateway_test}" == "1" || "${deepseek_e2e_test}" == "1" || "${managed_runtime}" == "1" ]]; then
  echo "Networking is restricted to the host Tool Gateway; model credentials remain outside the microVM."
else
  echo "The runtime image contains no model credentials and networking is not configured yet."
fi

if [[ "${managed_runtime}" == "1" ]]; then
  bridge_host="$(cmdline_value agent_bridge_host)"
  bridge_port="$(cmdline_value agent_bridge_port)"
  bridge_token="$(cmdline_value agent_bridge_token)"
  mcp_host="$(cmdline_value agent_mcp_host)"
  mcp_port="$(cmdline_value agent_mcp_port)"
  mcp_token="$(cmdline_value agent_mcp_token)"
  echo "AGENT_TASK_WORKER_READY bridge=${bridge_host}:${bridge_port} credential=instance-token"
  exec env \
    AGENT_BRIDGE_URL="http://${bridge_host}:${bridge_port}" \
    AGENT_BRIDGE_TOKEN="${bridge_token}" \
    AGENT_MCP_URL="http://${mcp_host}:${mcp_port}/mcp" \
    AGENT_MCP_TOKEN="${mcp_token}" \
    /usr/local/sbin/agent-task-worker
fi

trap 'poweroff -f' TERM INT
while true; do
  sleep 3600 &
  wait "$!"
done
