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

COORDINATION_LOCK_FILE="/run/pins-wifi-coordination.lock"
STATE_FILE="/run/pins-wifi-watchdog.failures"
WIFI_CONFIG_FILE="/opt/pinsdaemon/app/wifi_config.json"
WIFI_CONNECT_SCRIPT="/usr/local/bin/wifi-connect.sh"
LOG_TAG="pins-wifi-watchdog"
DEFAULT_WIFI_INTERFACE="wlan0"
LOCAL_LOG_DIR="${PINSDAEMON_LOG_DIR:-/opt/pinsdaemon/logs}"
LOCAL_LOG_RETENTION_DAYS="${PINSDAEMON_LOG_RETENTION_DAYS:-5}"

# At the default 10s timer interval, 3 consecutive failures is ~30 seconds
# of sustained unreachability before falling back, absorbing transient blips.
MAX_FAILURES="${PINS_WIFI_WATCHDOG_MAX_FAILURES:-3}"
PING_TIMEOUT_SECONDS="${PINS_WIFI_WATCHDOG_PING_TIMEOUT:-2}"

prune_local_logs() {
    mkdir -p "$LOCAL_LOG_DIR" 2>/dev/null || return 0
    find "$LOCAL_LOG_DIR" -maxdepth 1 -type f -name 'wifi-watchdog-*.log' -mtime +"$((LOCAL_LOG_RETENTION_DAYS - 1))" -delete 2>/dev/null || true
}

log() {
    local message="$*"
    logger -t "$LOG_TAG" "$message"
    prune_local_logs
    local log_file="$LOCAL_LOG_DIR/wifi-watchdog-$(date +%F).log"
    printf "%s %s\n" "$(date --iso-8601=seconds)" "$message" >>"$log_file" 2>/dev/null || true
    # This script runs as root; keep the log readable/owned by the daemon user
    # that packages diagnostics.
    chown sysupdate-api:sysupdate-api "$log_file" 2>/dev/null || true
    chmod 644 "$log_file" 2>/dev/null || true
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
hotspot = None
desired_mode = "auto"

if os.path.exists(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            candidate_client = data.get("client_interface")
            candidate_hotspot = data.get("hotspot_interface")
            candidate_mode = data.get("desired_mode")
            if isinstance(candidate_client, str):
                candidate_client = candidate_client.strip()
                if valid.fullmatch(candidate_client):
                    client = candidate_client
            if isinstance(candidate_hotspot, str):
                candidate_hotspot = candidate_hotspot.strip()
                if valid.fullmatch(candidate_hotspot):
                    hotspot = candidate_hotspot
            if isinstance(candidate_mode, str) and candidate_mode.strip().lower() in {"auto", "hotspot"}:
                desired_mode = candidate_mode.strip().lower()
    except Exception:
        pass

if not hotspot:
    hotspot = client

print(client)
print(hotspot)
print(desired_mode)
PY
}

mapfile -t IFACES < <(read_configured_interfaces)
CLIENT_IFACE="${IFACES[0]:-$DEFAULT_WIFI_INTERFACE}"
HOTSPOT_IFACE="${IFACES[1]:-$CLIENT_IFACE}"
DESIRED_MODE="${IFACES[2]:-auto}"

if ! command -v flock >/dev/null 2>&1; then
    log "Cannot run Wi-Fi watchdog safely: flock is unavailable"
    exit 0
fi

# The dispatcher and manual/API Wi-Fi path use this same kernel-managed lock.
# Holding it for the entire check and recovery operation prevents a client
# activation from racing an AP activation on a single Wi-Fi radio. The lock is
# automatically released if this process exits or is killed.
exec 9>"$COORDINATION_LOCK_FILE"
if ! flock -n 9; then
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

is_wifi_client_active() {
    nmcli -t -f NAME,TYPE,DEVICE connection show --active 2>/dev/null \
        | awk -F: -v iface="$CLIENT_IFACE" \
            '$2=="802-11-wireless" && $3==iface && $1!="Hotspot" && $1!="hotspot-ap" && $1!~ /^pins-/ {found=1} END {exit !found}'
}

if is_hotspot_active; then
    FAILURES="$(get_failures)"
    if [[ "$FAILURES" =~ ^[1-9][0-9]*$ ]]; then
        log "Fallback hotspot is active on ${HOTSPOT_IFACE}; recovery confirmed"
    fi
    set_failures 0
    exit 0
fi

# NetworkManager's dispatcher normally persists this manual override. This
# independent guard ensures a desktop/VNC client activation is not torn down
# even if that dispatcher event is delayed or unavailable.
if [[ "$DESIRED_MODE" == "hotspot" && "$CLIENT_IFACE" == "$HOTSPOT_IFACE" ]] \
    && is_wifi_client_active; then
    set_failures 0
    exit 0
fi

if [[ "$DESIRED_MODE" == "hotspot" ]]; then
    log "Persistent hotspot mode requested; enabling hotspot on ${HOTSPOT_IFACE}"
    if PINS_WIFI_COORDINATION_LOCK_HELD=1 "$WIFI_CONNECT_SCRIPT" --hotspot --client-iface "$CLIENT_IFACE" --hotspot-iface "$HOTSPOT_IFACE" >/dev/null 2>&1; then
        log "Persistent hotspot enabled successfully on ${HOTSPOT_IFACE}"
        set_failures 0
    else
        log "Failed to enable persistent hotspot on ${HOTSPOT_IFACE}; will retry"
        set_failures "$MAX_FAILURES"
    fi
    exit 0
fi

gateway_reachable() {
    local gateway
    gateway="$(ip -4 route show dev "$CLIENT_IFACE" default 2>/dev/null | awk '/via/ {print $3; exit}')"
    if [[ -z "$gateway" ]]; then
        # No default route (or no gateway on the default route) on the
        # client interface: not usably connected.
        return 1
    fi
    ping -I "$CLIENT_IFACE" -c 1 -W "$PING_TIMEOUT_SECONDS" "$gateway" >/dev/null 2>&1
}

if gateway_reachable; then
    FAILURES="$(get_failures)"
    if [[ "$FAILURES" =~ ^[1-9][0-9]*$ ]]; then
        log "Gateway connectivity restored on ${CLIENT_IFACE} after ${FAILURES} failed check(s)"
    fi
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
if PINS_WIFI_COORDINATION_LOCK_HELD=1 "$WIFI_CONNECT_SCRIPT" --hotspot --client-iface "$CLIENT_IFACE" --hotspot-iface "$HOTSPOT_IFACE" >/dev/null 2>&1; then
    log "Fallback hotspot enabled successfully on ${HOTSPOT_IFACE}"
    set_failures 0
else
    log "Failed to enable fallback hotspot on ${HOTSPOT_IFACE}; will retry"
    # Keep the threshold reached so the next timer run retries immediately
    # instead of waiting for another full failure-count cycle.
    set_failures "$MAX_FAILURES"
fi
exit 0
