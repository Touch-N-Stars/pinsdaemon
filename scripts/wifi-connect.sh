#!/bin/bash

HOTSPOT_CONFIG_FILE="${HOTSPOT_CONFIG_FILE:-/opt/pinsdaemon/app/hotspot_config.json}"
WIFI_CONFIG_FILE="${WIFI_CONFIG_FILE:-/opt/pinsdaemon/app/wifi_config.json}"
DEFAULT_HOTSPOT_PASSWORD="touchnstars"
MANUAL_CONNECT_LOCK_FILE="/run/pins-wifi-connect.lock"
DEFAULT_WIFI_INTERFACE="wlan0"
FORCE_HOTSPOT=false
CLIENT_IFACE=""
HOTSPOT_IFACE=""

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
            shift 2
            ;;
        --hotspot-iface)
            HOTSPOT_IFACE="$2"
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
BAND="${POSITIONAL_ARGS[2]:-}" # "a" for 5GHz, "bg" for 2.4GHz

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

echo "Using interfaces: client=$CLIENT_IFACE hotspot=$HOTSPOT_IFACE"

if [ "$FORCE_HOTSPOT" = true ]; then
    echo "Hotspot mode requested explicitly."
fi

touch "$MANUAL_CONNECT_LOCK_FILE" 2>/dev/null || true
trap 'rm -f "$MANUAL_CONNECT_LOCK_FILE"' EXIT

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

enable_hotspot() {
    echo "Connection failed (or forcing hotspot). Re-enabling hotspot..."

    # Ensure client mode is dropped before creating AP mode.
    nmcli device disconnect "$HOTSPOT_IFACE" >/dev/null 2>&1 || true

    # Remove legacy hotspot profiles so nmcli creates a fresh AP with current password.
    existing_hotspots=$(nmcli -t -f NAME,TYPE connection show 2>/dev/null | grep -E "^(Hotspot|hotspot-ap):802-11-wireless" | cut -d: -f1)
    if [ -n "$existing_hotspots" ]; then
        while IFS= read -r conn; do
            if [ -n "$conn" ]; then
                nmcli connection delete "$conn" >/dev/null 2>&1 || true
            fi
        done <<< "$existing_hotspots"
    fi
    
    # Get CPU ID for unique SSID
    CPU_ID="0000"
    if [ -f /proc/cpuinfo ]; then
        # Use user provided logic to extract serial
        CPU_ID=$(grep Serial /proc/cpuinfo | awk '{print substr($3, length($3)-4)}')
    fi
    
    # Fallback if empty
    if [ -z "$CPU_ID" ]; then
        CPU_ID="0000"
    fi

    HOTSPOT_SSID="pins-$CPU_ID"
    HOTSPOT_PASSWORD="$(get_hotspot_password)"

    echo "Creating hotspot: $HOTSPOT_SSID"

    # Create new hotspot with dynamic SSID.
    # NetworkManager can return "activation was enqueued" during transient state changes,
    # so retry briefly before giving up.
    HOTSPOT_ENABLED=0
    for attempt in 1 2 3; do
        if [ "$attempt" -gt 1 ]; then
            echo "Retrying hotspot activation ($attempt/3)..."
            sleep 2
        fi
        nmcli device disconnect "$HOTSPOT_IFACE" >/dev/null 2>&1 || true
        if nmcli device wifi hotspot ifname "$HOTSPOT_IFACE" ssid "$HOTSPOT_SSID" password "$HOTSPOT_PASSWORD"; then
            HOTSPOT_ENABLED=1
            break
        fi
    done

    if [ "$HOTSPOT_ENABLED" -eq 1 ]; then
        
        
        # Try finding the connection we just created (active on selected hotspot interface)
        NEW_CONN=$(nmcli -t -f NAME,DEVICE connection show --active | grep ":$HOTSPOT_IFACE" | cut -d: -f1 | head -n1)
        
        if [ -n "$NEW_CONN" ]; then
             echo "Configuring powersave for $NEW_CONN"
             nmcli connection modify "$NEW_CONN" 802-11-wireless.powersave 2 || true
        else
             # Fallback to hardcoded names just in case
             nmcli connection modify hotspot-ap 802-11-wireless.powersave 2 2>/dev/null || true
        fi

        # Extra safeguard: also disable kernel powersave flag for this device
        if command -v iw >/dev/null 2>&1; then
            iw dev "$HOTSPOT_IFACE" set power_save off || true
        fi
        
        echo "Hotspot enabled successfully."
    else
        echo "Failed to enable hotspot."
        return 1
    fi

    return 0
}

if [ "$FORCE_HOTSPOT" = true ]; then
    enable_hotspot
    exit $?
fi

if [ -z "$SSID" ]; then
    echo "Error: SSID is required."
    exit 1
fi

# Check if we are already connected to the target SSID
ACTIVE_SSID=$(nmcli -t -f NAME,TYPE,DEVICE connection show --active | grep ":802-11-wireless:$CLIENT_IFACE" | cut -d: -f1 | head -n1)

if [ "$ACTIVE_SSID" == "$SSID" ]; then
    # If band is specified, we check if we need to switch bands
    if [ -n "$BAND" ]; then
        echo "Already connected, but band preference selected ($BAND). Verifying settings..."
        # We proceed to standard connection logic to ensure band settings are applied
    else
        echo "Already connected to $SSID."
        exit 0
    fi
fi

echo "Preparing to connect to $SSID..."

# 0. Force a rescan to ensure we know the security type
# We run this in the background/wait briefly or just run it. 
# Sometimes rescan fails if busy, we ignore error.
nmcli device wifi rescan ifname "$CLIENT_IFACE" 2>/dev/null || true
# Give it a moment to populate
sleep 3

# 1. Remove existing hotspot connection if any
# Find any connections named "hotspot-ap" or starting with "Hotspot" (default nmcli naming)
echo "Cleaning up existing hotspot connections..."
existing_hotspots=$(nmcli -t -f NAME connection show | grep -E "^(Hotspot|hotspot-ap)")

if [ -n "$existing_hotspots" ]; then
    # Process each line to handle potential spaces in names
    while IFS= read -r conn; do
        if [ -n "$conn" ]; then
            echo "Removing hotspot connection: $conn"
            nmcli connection delete "$conn" || true
        fi
    done <<< "$existing_hotspots"
fi

# 2. Clean up any EXISTING profiles for the target SSID
# We only delete the profile if we actually intend to update it with a new password
# BUT, deleting it makes "device connect" rely purely on scan results, which can be flaky.
# Instead, we should try to modify the existing connection if it exists, or verify the network is visible.

if [ -n "$PASSWORD" ]; then
    # If a profile exists, we can try to update its password instead of deleting/recreating
    if nmcli connection show "$SSID" >/dev/null 2>&1; then
        echo "Updating existing connection profile for $SSID..."
        nmcli connection modify "$SSID" wifi-sec.psk "$PASSWORD" || true
        # Ensure WPA key management is present when using a password.
        nmcli connection modify "$SSID" wifi-sec.key-mgmt wpa-psk || true
    fi
fi

if nmcli connection show "$SSID" >/dev/null 2>&1; then
    nmcli connection modify "$SSID" connection.interface-name "$CLIENT_IFACE" || true
fi

# 3. Connect to the new wifi network
echo "Connecting to $SSID..."

CONNECT_SUCCESS=0

# Loop to retry connection if "No network found" occurs (scan timing issue)
MAX_RETRIES=2
count=0
CONNECT_SUCCESS=1 # Default to failure unless proven otherwise

while [ $count -lt $MAX_RETRIES ]; do
    # Logic to prefer existing connection
    # We try "connection up" first if profile exists (whether we just updated pw or not)
    # BUT, if we have a NEW password provided in arguments, "connection up" might use the OLD password stored in the profile
    # unless we successfully modified it above. If modification failed or didn't happen, we might need to be careful.
    # However, since we did 'nmcli connection modify' above, 'connection up' should use the new password.
    
    if nmcli connection show "$SSID" >/dev/null 2>&1; then
        echo "Found existing profile for $SSID. Attempting to bring it up..."
        if nmcli connection up "$SSID" ifname "$CLIENT_IFACE"; then
            CONNECT_SUCCESS=0
            break
        else
            echo "Failed to bring up existing connection." 
            # If we failed to bring it up, it might be due to wrong interface or other issues.
            # We will fall through to 'device wifi connect' which is more aggressive.
        fi
    fi

    # Fallback to device connect (creates new profile if missing, or updates existing if arguments provided)
        CMD=("nmcli" "device" "wifi" "connect" "$SSID" "ifname" "$CLIENT_IFACE")
    if [ -n "$PASSWORD" ]; then
         CMD+=("password" "$PASSWORD" "name" "$SSID")
    fi

    # Execute connection command (avoid logging sensitive arguments)
    if [ -n "$PASSWORD" ]; then
        echo "Executing: nmcli device wifi connect $SSID ifname $CLIENT_IFACE password *** name $SSID"
    else
        echo "Executing: nmcli device wifi connect $SSID ifname $CLIENT_IFACE"
    fi
    "${CMD[@]}" && { CONNECT_SUCCESS=0; break; } || {
        echo "Connection attempt failed. Retrying scan..."
        nmcli device wifi rescan ifname "$CLIENT_IFACE" 2>/dev/null || true
        # Wait a bit longer for scan results to propagate
        sleep 8
        count=$((count + 1))
    }
done

if [ $CONNECT_SUCCESS -ne 0 ]; then
   echo "Failed to connect to $SSID after multiple attempts."
   enable_hotspot || echo "Hotspot fallback failed."
   exit 1
fi

echo "Successfully connected to $SSID."

if [ -n "$BAND" ]; then
    echo "Applying band preference: $BAND"
    # Use 802-11-wireless.band for better compatibility
    if nmcli connection modify "$SSID" 802-11-wireless.band "$BAND"; then
        echo "Reactivating connection with band preference settings..."
        nmcli connection up "$SSID" ifname "$CLIENT_IFACE" || true
    else
        echo "Warning: Failed to set wifi band to $BAND"
    fi
fi

if [ $CONNECT_SUCCESS -ne 0 ]; then
    echo "Failed to connect to $SSID."
    enable_hotspot
    exit 1
fi

echo "Successfully connected to $SSID."
# Optional: Disable powersave on client connection too
# Filter specifically for wireless connections to avoid configuring ethernet connections
CURRENT_CONN=$(nmcli -t -f NAME,TYPE,DEVICE connection show --active | grep ":802-11-wireless:$CLIENT_IFACE" | cut -d: -f1 | head -n1)
if [ -n "$CURRENT_CONN" ]; then
    nmcli connection modify "$CURRENT_CONN" 802-11-wireless.powersave 2 || true
fi
exit 0

