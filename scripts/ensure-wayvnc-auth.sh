#!/bin/bash
# Keep WayVNC reachable after Raspberry Pi's hardened wayvnc package.
#
# PINS images ship /etc/wayvnc/config with enable_auth=false so the
# Touch-N-Stars VNC viewer (noVNC) can connect without credentials. Since
# wayvnc 0.10.2-1+rpt2, /usr/sbin/wayvnc-control.py disconnects every client
# whose authenticated username differs from the desktop session user, so
# unauthenticated clients are dropped immediately after connecting.
#
# When the installed control script enforces that rule, switch the config to
# PAM authentication (log in with the desktop user, e.g. pi). noVNC cannot do
# VeNCrypt X509 (TLS) or RSA-AES, so no TLS certificate is configured and
# relax_encryption enables Apple DH (security type 30), which noVNC supports.
# Desktop viewers such as RealVNC keep using RSA-AES.
#
# Runs as root (ExecStartPre=+ of sysupdate-api.service and after upgrades).
# Idempotent; it only rewrites configs that still have enable_auth=false or
# that this script wrote earlier.
set -euo pipefail

CONFIG="${WAYVNC_CONFIG:-/etc/wayvnc/config}"
CONTROL_SCRIPT="${WAYVNC_CONTROL_SCRIPT:-/usr/sbin/wayvnc-control.py}"
RSA_KEY="${WAYVNC_RSA_KEY:-/etc/wayvnc/rsa_key.pem}"
RESTART="${WAYVNC_RESTART:-1}"
MARKER="# Managed by pinsdaemon ensure-wayvnc-auth.sh"

log() { echo "ensure-wayvnc-auth: $*"; }

if [[ ! -f "$CONFIG" ]]; then
    log "no $CONFIG; nothing to do"
    exit 0
fi

if [[ ! -f "$CONTROL_SCRIPT" ]] || ! grep -q "client-disconnect" "$CONTROL_SCRIPT"; then
    log "wayvnc control does not enforce user-only access; leaving config unchanged"
    exit 0
fi

config_value() {
    # Last assignment wins, like wayvnc's own parser.
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$CONFIG" | tail -n 1 | tr -d '[:space:]'
}

managed=false
grep -qF "$MARKER" "$CONFIG" && managed=true
auth="$(config_value enable_auth)"

if [[ "$managed" != "true" && "$auth" != "false" && -n "$auth" ]]; then
    log "custom authenticated config detected; leaving it unchanged"
    exit 0
fi

address="$(config_value address)"
port="$(config_value port)"
[[ -n "$address" ]] || address="0.0.0.0"
[[ -n "$port" ]] || port="5900"

new_config="$(mktemp)"
trap 'rm -f "$new_config"' EXIT
{
    echo "$MARKER"
    echo "address=$address"
    echo "port=$port"
    echo "enable_auth=true"
    echo "enable_pam=true"
    echo "relax_encryption=true"
    if [[ -f "$RSA_KEY" ]]; then
        echo "rsa_private_key_file=$RSA_KEY"
    fi
} > "$new_config"

if cmp -s "$new_config" "$CONFIG"; then
    log "config already up to date"
    exit 0
fi

if [[ "$managed" != "true" ]]; then
    cp -p "$CONFIG" "$CONFIG.pins-noauth.bak"
fi
install -m 0644 "$new_config" "$CONFIG"
log "enabled PAM authentication in $CONFIG (log in with the desktop user)"

if [[ "$RESTART" == "1" ]] && command -v systemctl >/dev/null 2>&1 \
        && systemctl is-enabled --quiet wayvnc.service 2>/dev/null; then
    systemctl try-restart wayvnc.service || log "wayvnc restart failed"
fi
