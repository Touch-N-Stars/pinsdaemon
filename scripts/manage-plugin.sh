#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-}"
PACKAGE_NAME="${2:-}"
PINS_SERVICE_RESTART_CANDIDATES="${PINS_SERVICE_RESTART_CANDIDATES:-pins.service sysupdate-api.service}"

ALLOWED_PLUGINS=(
    "pins-plugin-alpaca"
    "pins-plugin-groundstation"
    "pins-plugin-joko"
    "pins-plugin-livestack"
    "pins-plugin-nightsummary"
    "pins-plugin-ninaapi"
    "pins-plugin-orbitals"
    "pins-plugin-orbuculum"
    "pins-plugin-phd2tools"
    "pins-plugin-pins"
    "pins-plugin-polaralignment"
    "pins-plugin-tenmicron"
    "pins-plugin-touch-n-stars"
)

if [[ -z "$ACTION" || -z "$PACKAGE_NAME" ]]; then
    echo "Usage: $0 <install|uninstall> <package-name>"
    exit 1
fi

case "$ACTION" in
    install|uninstall)
        ;;
    *)
        echo "Unsupported action: $ACTION"
        exit 1
        ;;
esac

is_allowed=false
for plugin in "${ALLOWED_PLUGINS[@]}"; do
    if [[ "$plugin" == "$PACKAGE_NAME" ]]; then
        is_allowed=true
        break
    fi
done

if [[ "$is_allowed" != true ]]; then
    echo "Package is not an allowed plugin: $PACKAGE_NAME"
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive

restart_pins_service() {
    local service_list
    local service_name
    local load_state

    service_list="$PINS_SERVICE_RESTART_CANDIDATES"
    for service_name in $service_list; do
        if [[ "$service_name" != *.service ]]; then
            service_name="${service_name}.service"
        fi

        load_state="$(systemctl show -p LoadState --value "$service_name" 2>/dev/null || true)"
        if [[ "$load_state" != "loaded" ]]; then
            continue
        fi

        echo "Restarting service: $service_name"
        systemctl restart "$service_name"
        echo "Service restarted successfully: $service_name"
        return 0
    done

    echo "Failed to find a loaded PINS service to restart. Checked: $service_list" >&2
    return 1
}

if [[ "$ACTION" == "install" ]]; then
    echo "Installing plugin package: $PACKAGE_NAME"
    apt-get update
    apt-get install -y "$PACKAGE_NAME"
    echo "Plugin installed successfully: $PACKAGE_NAME"
    exit 0
fi

echo "Uninstalling plugin package: $PACKAGE_NAME"
apt-get remove -y "$PACKAGE_NAME"
echo "Plugin uninstalled successfully: $PACKAGE_NAME"
restart_pins_service
