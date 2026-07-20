#!/usr/bin/env bash
set -euo pipefail

# Active liveness check, independent of NetworkManager dispatcher events.
#
# scripts/90-pins-wifi-recovery only runs when NetworkManager itself decides
# to fire a dispatcher action (down/dhcp4-change/connectivity-change/reapply)
# for the client interface. If the Wi-Fi STA stays associated (nmcli reports
# "connected") while the router/AP behind it is actually dead, NetworkManager
# never fires such an action, so that script never runs and the fallback
# hotspot never comes up. This script is invoked on a fixed timer instead, so
# it does not depend on NetworkManager noticing anything.

LOCK_DIR="/run/pins-wifi-watchdog.lock"
DISPATCHER_LOCK_DIR="/run/pins-wifi-recovery.lock"
STATE_FILE="/run/pins-wifi-watchdog.failures"
WIFI_CONFIG_FILE="/opt/pinsdaemon/app/wifi_config.json"
WIFI_CONNECT_SCRIPT="/usr/local/bin/wifi-connect.sh"
MANUAL_CONNECT_LOCK_FILE="/run/pins-wifi-connect.lock"
LOG_TAG="pins-wifi-watchdog"
DEFAULT_WIFI_INTERFACE="wlan0"
LOCAL_LOG_DIR="${PINSDAEMON_LOG_DIR:-/opt/pinsdaemon/logs}"
LOCAL_LOG_RETENTION_DAYS="${PINSDAEMON_LOG_RETENTION_DAYS:-5}"

# At the default 30s timer interval, 5 consecutive failures is ~2.5 minutes
# of sustained unreachability before falling back, absorbing transient blips.
MAX_FAILURES="${PINS_WIFI_WATCHDOG_MAX_FAILURES:-5}"
PING_TIMEOUT_SECONDS="${PINS_WIFI_WATCHDOG_PING_TIMEOUT:-2}"

prune_local_logs() {
    mkdir -p "$LOCAL_LOG_DIR" 2>/dev/null || return 0
    find "$LOCAL_LOG_DIR" -maxdepth 1 -type f -name 'wifi-watchdog-*.log' -mtime +"$((LOCAL_LOG_RETENTION_DAYS - 1))" -delete 2>/dev/null || true
}

log() {
    local message="$*"
    logger -t "$LOG_TAG" "$message"
    prune_local_logs
    printf "%s %s\n" "$(date --iso-8601=seconds)" "$message" >>"$LOCAL_LOG_DIR/wifi-watchdog-$(date +%F).log" 2>/dev/null || true
}

read_configured_interfaces() {
    python3 - "$WIFI_CONFIG_FILE" "$DEFAULT_WIFI_INTERFACE" <<'PY'
import json
import os
import re
import sys

path = sys.argv[1]
default_iface = sys.argv[2]
valid = re.compile(r"^[A-Za-z0-9._-]+$")

client = default_iface
hotspot = default_iface

if os.path.exists(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            candidate_client = data.get("client_interface")
            candidate_hotspot = data.get("hotspot_interface")
            if isinstance(candidate_client, str):
                candidate_client = candidate_client.strip()
                if valid.fullmatch(candidate_client):
                    client = candidate_client
            if isinstance(candidate_hotspot, str):
                candidate_hotspot = candidate_hotspot.strip()
                if valid.fullmatch(candidate_hotspot):
                    hotspot = candidate_hotspot
    except Exception:
        pass

if not hotspot:
    hotspot = client

print(client)
print(hotspot)
PY
}

mapfile -t IFACES < <(read_configured_interfaces)
CLIENT_IFACE="${IFACES[0]:-$DEFAULT_WIFI_INTERFACE}"
HOTSPOT_IFACE="${IFACES[1]:-$CLIENT_IFACE}"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    # Previous run still in flight (should not happen at 30s cadence with a
    # short ping timeout, but avoid overlapping runs just in case).
    exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

if [[ -d "$DISPATCHER_LOCK_DIR" ]]; then
    # 90-pins-wifi-recovery is actively handling a transition; don't race it.
    exit 0
fi

if [[ -f "$MANUAL_CONNECT_LOCK_FILE" ]]; then
    # Manual wifi-connect run is in progress; avoid competing NM operations.
    exit 0
fi

get_failures() {
    if [[ -f "$STATE_FILE" ]]; then
        cat "$STATE_FILE" 2>/dev/null || echo "0"
    else
        echo "0"
    fi
}

set_failures() {
    printf "%s\n" "$1" > "$STATE_FILE"
}

is_hotspot_active() {
    nmcli -t -f NAME,TYPE,DEVICE connection show --active 2>/dev/null \
        | awk -F: -v iface="$HOTSPOT_IFACE" '$2=="802-11-wireless" && $3==iface {print $1}' \
        | grep -E '^(Hotspot|hotspot-ap|pins-)' >/dev/null 2>&1
}

if is_hotspot_active; then
    set_failures 0
    exit 0
fi

gateway_reachable() {
    local gateway
    gateway="$(ip -4 route show dev "$CLIENT_IFACE" default 2>/dev/null | awk '{print $3; exit}')"
    if [[ -z "$gateway" ]]; then
        # No default route on the client interface at all: not connected.
        return 1
    fi
    ping -I "$CLIENT_IFACE" -c 1 -W "$PING_TIMEOUT_SECONDS" "$gateway" >/dev/null 2>&1
}

if gateway_reachable; then
    set_failures 0
    exit 0
fi

FAILURES="$(get_failures)"
if ! [[ "$FAILURES" =~ ^[0-9]+$ ]]; then
    FAILURES=0
fi
FAILURES=$((FAILURES + 1))
set_failures "$FAILURES"

if [[ "$FAILURES" -lt "$MAX_FAILURES" ]]; then
    log "Gateway unreachable on ${CLIENT_IFACE} (check ${FAILURES}/${MAX_FAILURES}); waiting"
    exit 0
fi

log "Gateway unreachable on ${CLIENT_IFACE} after ${MAX_FAILURES} consecutive checks; enabling fallback hotspot on ${HOTSPOT_IFACE}"
"$WIFI_CONNECT_SCRIPT" --hotspot --client-iface "$CLIENT_IFACE" --hotspot-iface "$HOTSPOT_IFACE" >/dev/null 2>&1 || log "Failed to enable fallback hotspot"
set_failures 0
exit 0
