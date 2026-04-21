#!/usr/bin/env bash
set -euo pipefail

REQUIRED_PACKAGES=(
    "pins-nitecrawlersdk"
    "pins-wanderercoversdk"
    "pins-wandereretasdk"
    "pins-wandererrotatorsdk"
)

MISSING_PACKAGES=()

for pkg in "${REQUIRED_PACKAGES[@]}"; do
    if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
        echo "Package already installed: $pkg"
    else
        MISSING_PACKAGES+=("$pkg")
    fi
done

if [[ "${#MISSING_PACKAGES[@]}" -eq 0 ]]; then
    echo "All required PINS packages are already installed."
    exit 0
fi

echo "Missing packages detected: ${MISSING_PACKAGES[*]}"
echo "Updating apt metadata..."
export DEBIAN_FRONTEND=noninteractive
apt-get update

echo "Installing missing packages..."
apt-get install -y "${MISSING_PACKAGES[@]}"

echo "Required package check completed successfully."
