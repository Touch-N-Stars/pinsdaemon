#!/bin/bash

HOTSPOT_CONFIG_FILE="${HOTSPOT_CONFIG_FILE:-/opt/pinsdaemon/app/hotspot_config.json}"
WIFI_CONFIG_FILE="${WIFI_CONFIG_FILE:-/opt/pinsdaemon/app/wifi_config.json}"
DEFAULT_HOTSPOT_PASSWORD="touchnstars"
COORDINATION_LOCK_FILE="/run/pins-wifi-coordination.lock"
COORDINATION_LOCK_WAIT_SECONDS="${PINS_WIFI_COORDINATION_LOCK_WAIT_SECONDS:-30}"
DEFAULT_WIFI_INTERFACE="wlan0"
RIG_NAME_COMMAND="${PINS_RIG_NAME_COMMAND:-/usr/local/bin/pins-rig-name}"
WIFI_PROFILE_COMMAND="${PINS_WIFI_PROFILE_COMMAND:-/usr/local/bin/pins-wifi-profile.py}"
NM_DNSMASQ_SHARED_DIR="/etc/NetworkManager/dnsmasq-shared.d"
PINS_LOCAL_ONLY_DHCP_CONF="$NM_DNSMASQ_SHARED_DIR/pins-local-only.conf"
HOTSPOT_IPV4_CIDR="${PINS_HOTSPOT_IPV4_CIDR:-10.42.0.1/24}"
HOTSPOT_IPV4_ADDRESS="${HOTSPOT_IPV4_CIDR%%/*}"
HOTSPOT_AUTOCONNECT_PRIORITY="${PINS_HOTSPOT_AUTOCONNECT_PRIORITY:-0}"
FORCE_HOTSPOT=false
CLIENT_IFACE=""
HOTSPOT_IFACE=""
PARALLEL_WIFI_MODE=false
EXPLICIT_CLIENT_IFACE=false
EXPLICIT_HOTSPOT_IFACE=false
PASSWORD_STDIN=false
AUTO_CONNECT="no"

# Parse flags while keeping positional support for backward compatibility.
POSITIONAL_ARGS=()
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --hotspot)
            FORCE_HOTSPOT=true
            shift
            ;;
        --client-iface)
            CLIENT_IFACE="$2"
            EXPLICIT_CLIENT_IFACE=true
            shift 2
            ;;
        --hotspot-iface)
            HOTSPOT_IFACE="$2"
            EXPLICIT_HOTSPOT_IFACE=true
            shift 2
            ;;
        --password-stdin)
            PASSWORD_STDIN=true
            shift
            ;;
        --auto-connect)
            AUTO_CONNECT="$2"
            shift 2
            ;;
        --band)
            BAND="$2"
            shift 2
            ;;
        --)
            shift
            while [[ "$#" -gt 0 ]]; do
                POSITIONAL_ARGS+=("$1")
                shift
            done
            ;;
        *)
            POSITIONAL_ARGS+=("$1")
            shift
            ;;
    esac
done

SSID="${POSITIONAL_ARGS[0]:-}"
PASSWORD="${POSITIONAL_ARGS[1]:-}"
BAND="${BAND:-${POSITIONAL_ARGS[2]:-}}" # "a" for 5GHz, "bg" for 2.4GHz

if [ "$PASSWORD_STDIN" = true ]; then
    IFS= read -r PASSWORD || PASSWORD=""
fi

if [ "$AUTO_CONNECT" != "yes" ]; then
    AUTO_CONNECT="no"
fi

get_wifi_interface_from_config() {
    local key="$1"

    if [ -f "$WIFI_CONFIG_FILE" ] && command -v python3 >/dev/null 2>&1; then
        python3 - "$WIFI_CONFIG_FILE" "$key" <<'PY'
import json
import re
import sys

path = sys.argv[1]
key = sys.argv[2]
valid = re.compile(r"^[A-Za-z0-9._-]+$")

try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    raise SystemExit(0)

value = data.get(key)
if isinstance(value, str):
    value = value.strip()
    if valid.fullmatch(value):
        print(value)
PY
    fi
}

validate_or_fallback_interface() {
    local requested="$1"
    local fallback="$2"
    local label="$3"

    if [ -n "$requested" ] && nmcli device status 2>/dev/null | awk '{print $1}' | grep -qx "$requested"; then
        printf "%s" "$requested"
        return
    fi

    if [ -n "$requested" ]; then
        echo "Warning: requested $label interface '$requested' not found. Falling back to $fallback"
    fi
    printf "%s" "$fallback"
}

find_secondary_wifi_interface() {
    local primary="$1"

    nmcli -t -f DEVICE,TYPE device status 2>/dev/null \
        | awk -F: '$2=="wifi" {print $1}' \
        | grep -vx "$primary" \
        | head -n1
}

is_hotspot_active_on_interface() {
    local iface="$1"

    while IFS=: read -r profile_uuid profile_type profile_iface; do
        [ "$profile_type" = "802-11-wireless" ] || continue
        [ "$profile_iface" = "$iface" ] || continue
        [ "$(nmcli -g 802-11-wireless.mode connection show uuid "$profile_uuid" 2>/dev/null || true)" = "ap" ] && return 0
    done < <(nmcli -t -f UUID,TYPE,DEVICE connection show --active 2>/dev/null)
    return 1
}

list_hotspot_profile_uuids() {
    while IFS=: read -r profile_uuid profile_type; do
        [ "$profile_type" = "802-11-wireless" ] || continue
        [ -n "$profile_uuid" ] || continue
        if [ "$(nmcli -g 802-11-wireless.mode connection show uuid "$profile_uuid" 2>/dev/null || true)" = "ap" ]; then
            printf '%s\n' "$profile_uuid"
        fi
    done < <(nmcli -t -f UUID,TYPE connection show 2>/dev/null)
}

deactivate_hotspot_on_interface() {
    local iface="$1"
    while IFS=: read -r profile_uuid profile_type profile_iface; do
        [ "$profile_type" = "802-11-wireless" ] || continue
        [ "$profile_iface" = "$iface" ] || continue
        if [ "$(nmcli -g 802-11-wireless.mode connection show uuid "$profile_uuid" 2>/dev/null || true)" = "ap" ]; then
            nmcli connection down uuid "$profile_uuid" >/dev/null 2>&1 || true
        fi
    done < <(nmcli -t -f UUID,TYPE,DEVICE connection show --active 2>/dev/null)
}

deactivate_client_on_interface() {
    local iface="$1"

    echo "Disconnecting client Wi-Fi on $iface for hotspot-only mode."
    while IFS=: read -r profile_uuid profile_type profile_iface; do
        [ "$profile_type" = "802-11-wireless" ] || continue
        [ "$profile_iface" = "$iface" ] || continue
        if [ "$(nmcli -g 802-11-wireless.mode connection show uuid "$profile_uuid" 2>/dev/null || true)" != "ap" ]; then
            nmcli connection down uuid "$profile_uuid" >/dev/null 2>&1 || true
        fi
    done < <(nmcli -t -f UUID,TYPE,DEVICE connection show --active 2>/dev/null)

    # Prevent NetworkManager from immediately auto-activating another client
    # profile on this device. A later explicit connect re-enables the device.
    nmcli device disconnect "$iface" >/dev/null 2>&1 || true
}

hotspot_postcondition_met() {
    local iface="$1"
    is_hotspot_active_on_interface "$iface" || return 1
    nmcli -g IP4.ADDRESS device show "$iface" 2>/dev/null \
        | grep -qE "^${HOTSPOT_IPV4_ADDRESS//./\\.}/[0-9]+$"
}

ensure_local_only_hotspot_dhcp() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "Warning: cannot configure local-only hotspot DHCP without root privileges."
        return 0
    fi

    mkdir -p "$NM_DNSMASQ_SHARED_DIR" || {
        echo "Warning: failed to create $NM_DNSMASQ_SHARED_DIR"
        return 0
    }

cat > "$PINS_LOCAL_ONLY_DHCP_CONF" <<'EOF'
# PINS fallback hotspot is local-only: hand out an address, but no gateway/DNS.
# This keeps phones using LTE/5G for Internet while connected to the device AP.
port=0
dhcp-option=3
dhcp-option=6
EOF
    chmod 644 "$PINS_LOCAL_ONLY_DHCP_CONF" || true
}

if [ -z "$CLIENT_IFACE" ]; then
    CLIENT_IFACE="$(get_wifi_interface_from_config "client_interface")"
fi
if [ -z "$HOTSPOT_IFACE" ]; then
    HOTSPOT_IFACE="$(get_wifi_interface_from_config "hotspot_interface")"
fi

if [ -z "$CLIENT_IFACE" ]; then
    CLIENT_IFACE="$DEFAULT_WIFI_INTERFACE"
fi
if [ -z "$HOTSPOT_IFACE" ]; then
    HOTSPOT_IFACE="$CLIENT_IFACE"
fi

CLIENT_IFACE="$(validate_or_fallback_interface "$CLIENT_IFACE" "$DEFAULT_WIFI_INTERFACE" "client")"
HOTSPOT_IFACE="$(validate_or_fallback_interface "$HOTSPOT_IFACE" "$CLIENT_IFACE" "hotspot")"

# If only one interface is configured but another Wi-Fi adapter is available,
# keep client and hotspot on separate adapters.
if [ "$CLIENT_IFACE" = "$HOTSPOT_IFACE" ]; then
    if [ "$EXPLICIT_CLIENT_IFACE" = true ] && [ "$EXPLICIT_HOTSPOT_IFACE" = true ]; then
        echo "Using explicit interface selection from caller: client=$CLIENT_IFACE hotspot=$HOTSPOT_IFACE"
    else
        SECONDARY_IFACE="$(find_secondary_wifi_interface "$CLIENT_IFACE")"
        if [ -n "$SECONDARY_IFACE" ]; then
            HOTSPOT_IFACE="$SECONDARY_IFACE"
            PARALLEL_WIFI_MODE=true
            echo "Detected secondary Wi-Fi adapter ($SECONDARY_IFACE). Enabling parallel client+hotspot mode."
        fi
    fi
fi

if [ "$CLIENT_IFACE" != "$HOTSPOT_IFACE" ]; then
    PARALLEL_WIFI_MODE=true
fi

echo "Using interfaces: client=$CLIENT_IFACE hotspot=$HOTSPOT_IFACE"

if [ "$FORCE_HOTSPOT" = true ]; then
    echo "Hotspot mode requested explicitly."
fi

# Recovery callers already hold descriptor 9 for this lock. Manual/API calls
# wait briefly for an in-flight recovery operation rather than issuing
# competing nmcli commands on the same radio.
if ! [[ "$COORDINATION_LOCK_WAIT_SECONDS" =~ ^[0-9]+$ ]]; then
    COORDINATION_LOCK_WAIT_SECONDS=30
fi

INHERITED_COORDINATION_LOCK=false
if [ "${PINS_WIFI_COORDINATION_LOCK_HELD:-0}" = "1" ] \
    && [ -e "/proc/$$/fd/9" ] \
    && [ "$(readlink -f "/proc/$$/fd/9" 2>/dev/null || true)" = "$(readlink -f "$COORDINATION_LOCK_FILE" 2>/dev/null || true)" ]; then
    INHERITED_COORDINATION_LOCK=true
fi

if [ "$INHERITED_COORDINATION_LOCK" != true ]; then
    if ! command -v flock >/dev/null 2>&1; then
        echo "Error: cannot coordinate Wi-Fi operation because flock is unavailable."
        exit 1
    fi
    exec 9>"$COORDINATION_LOCK_FILE"
    if ! flock -w "$COORDINATION_LOCK_WAIT_SECONDS" 9; then
        echo "Error: another Wi-Fi recovery operation is still in progress."
        exit 1
    fi
fi

get_hotspot_password() {
    local hotspot_password="$DEFAULT_HOTSPOT_PASSWORD"

    if [ -f "$HOTSPOT_CONFIG_FILE" ] && command -v python3 >/dev/null 2>&1; then
        local configured_password
        configured_password=$(python3 - "$HOTSPOT_CONFIG_FILE" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    password = data.get("password", "")
    if isinstance(password, str):
        password = password.strip()
    else:
        password = ""
    if 8 <= len(password) <= 63:
        print(password)
except Exception:
    pass
PY
)

        if [ -n "$configured_password" ]; then
            hotspot_password="$configured_password"
        fi
    fi

    printf "%s" "$hotspot_password"
}

get_hotspot_band() {
    if [ -f "$HOTSPOT_CONFIG_FILE" ] && command -v python3 >/dev/null 2>&1; then
        python3 - "$HOTSPOT_CONFIG_FILE" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    raise SystemExit(0)

band = data.get("band")
if not isinstance(band, str):
    raise SystemExit(0)

candidate = band.strip().lower()
if candidate in {"2.4ghz", "bg"}:
    print("bg")
elif candidate in {"5ghz", "a"}:
    print("a")
PY
    fi
}

get_hotspot_channel() {
    if [ -f "$HOTSPOT_CONFIG_FILE" ] && command -v python3 >/dev/null 2>&1; then
        python3 - "$HOTSPOT_CONFIG_FILE" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    raise SystemExit(0)

channel = data.get("channel")
if isinstance(channel, bool):
    raise SystemExit(0)
try:
    channel = int(channel)
except Exception:
    raise SystemExit(0)

if channel > 0:
    print(channel)
PY
    fi
}

enable_hotspot() {
    echo "Connection failed (or forcing hotspot). Re-enabling hotspot..."

    ensure_local_only_hotspot_dhcp

    # Ensure client mode is dropped before creating AP mode.
    nmcli device disconnect "$HOTSPOT_IFACE" >/dev/null 2>&1 || true

    # Remove legacy hotspot profiles so nmcli creates a fresh AP with current password.
    existing_hotspots=$(list_hotspot_profile_uuids)
    if [ -n "$existing_hotspots" ]; then
        while IFS= read -r conn; do
            if [ -n "$conn" ]; then
                nmcli connection delete uuid "$conn" >/dev/null 2>&1 || true
            fi
        done <<< "$existing_hotspots"
    fi
    
    # The hotspot SSID and mDNS hostname share one persisted hardware identity.
    # This makes each rig unambiguous on networks containing multiple PINS units.
    if [ -x "$RIG_NAME_COMMAND" ]; then
        HOTSPOT_SSID="$("$RIG_NAME_COMMAND")"
    else
        CPU_ID="$(sed -n 's/^Serial[[:space:]]*:[[:space:]]*//p' /proc/cpuinfo 2>/dev/null \
            | tr -cd '0-9A-Fa-f' | sed 's/.*\(.....\)$/\1/')"
        [ -n "$CPU_ID" ] || CPU_ID="00000"
        HOTSPOT_SSID="pins-$(printf '%s' "$CPU_ID" | tr '[:upper:]' '[:lower:]')"
    fi
    HOTSPOT_PASSWORD="$(get_hotspot_password)"
    HOTSPOT_BAND="$(get_hotspot_band)"
    HOTSPOT_CHANNEL="$(get_hotspot_channel)"

    echo "Creating hotspot: $HOTSPOT_SSID"

    # Build the complete AP profile before activating it. The nmcli hotspot
    # shortcut activates a temporary shared profile immediately; modifying and
    # reactivating it can race NetworkManager's previous dnsmasq child, leaving
    # port 53/67 occupied. One fully configured activation avoids that race.
    HOTSPOT_PROFILE_UUID="$(cat /proc/sys/kernel/random/uuid)"
    if ! nmcli connection add \
        type wifi \
        ifname "$HOTSPOT_IFACE" \
        con-name Hotspot \
        ssid "$HOTSPOT_SSID" \
        connection.uuid "$HOTSPOT_PROFILE_UUID" \
        connection.autoconnect yes \
        connection.autoconnect-priority "$HOTSPOT_AUTOCONNECT_PRIORITY" \
        802-11-wireless.mode ap \
        802-11-wireless.powersave 2 \
        ipv4.method shared \
        ipv4.addresses "$HOTSPOT_IPV4_CIDR" \
        ipv6.method disabled \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$HOTSPOT_PASSWORD" >/dev/null; then
        echo "Failed to create configured hotspot profile."
        return 1
    fi

    if [ -n "$HOTSPOT_BAND" ]; then
        nmcli connection modify uuid "$HOTSPOT_PROFILE_UUID" 802-11-wireless.band "$HOTSPOT_BAND" || true
    fi
    if [ -n "$HOTSPOT_CHANNEL" ]; then
        nmcli connection modify uuid "$HOTSPOT_PROFILE_UUID" 802-11-wireless.channel "$HOTSPOT_CHANNEL" || true
    fi

    for attempt in 1 2 3; do
        if [ "$attempt" -gt 1 ]; then
            echo "Retrying hotspot activation ($attempt/3)..."
            sleep 2
        fi

        if nmcli connection up uuid "$HOTSPOT_PROFILE_UUID" ifname "$HOTSPOT_IFACE" >/dev/null 2>&1; then
            for _ in 1 2 3 4 5; do
                if hotspot_postcondition_met "$HOTSPOT_IFACE"; then
                    if command -v iw >/dev/null 2>&1; then
                        iw dev "$HOTSPOT_IFACE" set power_save off || true
                    fi
                    echo "Hotspot enabled successfully at $HOTSPOT_IPV4_ADDRESS."
                    return 0
                fi
                sleep 1
            done
        fi

        nmcli connection down uuid "$HOTSPOT_PROFILE_UUID" >/dev/null 2>&1 || true
        nmcli device disconnect "$HOTSPOT_IFACE" >/dev/null 2>&1 || true
    done

    echo "Hotspot activation did not reach the required AP/IP postcondition."
    return 1

}

if [ "$FORCE_HOTSPOT" = true ]; then
    deactivate_client_on_interface "$CLIENT_IFACE"
    enable_hotspot
    exit $?
fi

if [ -z "$SSID" ]; then
    echo "Error: SSID is required."
    exit 1
fi

echo "Preparing managed client Wi-Fi connection..."

# 0. A single radio cannot scan/connect while it is still serving the AP.
# Deactivate and remove the hotspot profile before asking NetworkManager to
# return that radio to managed/client mode.
if [ "$PARALLEL_WIFI_MODE" = false ]; then
    echo "Stopping single-adapter hotspot before client scan..."
    existing_hotspots=$(list_hotspot_profile_uuids)

    if [ -n "$existing_hotspots" ]; then
        while IFS= read -r conn; do
            if [ -n "$conn" ]; then
                nmcli connection down uuid "$conn" >/dev/null 2>&1 || true
                echo "Removing single-adapter hotspot profile."
                nmcli connection delete uuid "$conn" || true
            fi
        done <<< "$existing_hotspots"
    fi
    nmcli device disconnect "$CLIENT_IFACE" >/dev/null 2>&1 || true
fi

if [ "$PARALLEL_WIFI_MODE" = true ]; then
    deactivate_hotspot_on_interface "$CLIENT_IFACE"
    if ! is_hotspot_active_on_interface "$HOTSPOT_IFACE"; then
        echo "Parallel Wi-Fi mode: moving fallback hotspot to $HOTSPOT_IFACE."
        if ! enable_hotspot; then
            echo "PINS_WIFI_RESULT code=HOTSPOT_SWITCH_FAILED message=Could_not_prepare_dedicated_hotspot_adapter"
            exit 1
        fi
    fi
    echo "Parallel Wi-Fi mode active. Keeping hotspot on $HOTSPOT_IFACE."
fi

if [ ! -x "$WIFI_PROFILE_COMMAND" ]; then
    echo "PINS_WIFI_RESULT code=UNKNOWN message=Wi-Fi_profile_manager_is_unavailable"
    enable_hotspot || echo "PINS_WIFI_RESULT code=HOTSPOT_SWITCH_FAILED message=Hotspot_rollback_failed"
    exit 1
fi

if ! printf '%s\n' "$PASSWORD" | "$WIFI_PROFILE_COMMAND" \
    --client-iface "$CLIENT_IFACE" \
    --hotspot-iface "$HOTSPOT_IFACE" \
    --ssid "$SSID" \
    --band "$BAND" \
    --auto-connect "$AUTO_CONNECT" \
    --config-file "$WIFI_CONFIG_FILE"; then
    PASSWORD=""
    if ! enable_hotspot; then
        echo "PINS_WIFI_RESULT code=HOTSPOT_SWITCH_FAILED message=Hotspot_rollback_failed"
    fi
    exit 1
fi
PASSWORD=""

echo "Client Wi-Fi connection verified."

if [ "$PARALLEL_WIFI_MODE" = true ]; then
    if ! is_hotspot_active_on_interface "$HOTSPOT_IFACE"; then
        echo "Parallel mode: hotspot not active on $HOTSPOT_IFACE. Starting hotspot..."
        enable_hotspot || echo "Warning: failed to enable hotspot in parallel mode."
    else
        echo "Parallel mode: hotspot already active on $HOTSPOT_IFACE."
    fi
fi

exit 0
