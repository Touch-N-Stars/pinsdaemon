#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-}"
PACKAGE_NAME="${2:-}"

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
