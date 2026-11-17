#!/usr/bin/env bash
# Installs packages that pinsdaemon needs at runtime but must not declare as new
# Depends/Recommends: PINS updaters in the field that run a plain
# `apt-get upgrade` (system-upgrade.sh before --with-new-pkgs) keep back a
# pinsdaemon version that adds either relation, so the package carrying the
# fixed updater would never reach them. Runs as root on every daemon start.
set -euo pipefail

REQUIRED_PACKAGES=(
    "pins-nitecrawlersdk"
    "pins-wanderercoversdk"
    "pins-wandereretasdk"
    "pins-wandererrotatorsdk"
    # avahi-publish-service for the Linux Touch-N-Stars plugin's mDNS record.
    "avahi-utils"
)

LOCK_FILE="${PINS_REPOSITORY_LOCK:-/run/lock/pins-repository.lock}"
# The daemon is restarted by the very upgrade that installs it; wait for that
# upgrade to finish instead of racing it for the APT/dpkg locks.
LOCK_WAIT_SECONDS="${PINS_ENSURE_LOCK_WAIT_SECONDS:-3600}"
UPGRADE_WAIT_SECONDS="${PINS_ENSURE_UPGRADE_WAIT_SECONDS:-3600}"
POLL_SECONDS="${PINS_ENSURE_POLL_SECONDS:-5}"

is_installed() {
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

find_missing_packages() {
    local pkg
    MISSING_PACKAGES=()
    for pkg in "${REQUIRED_PACKAGES[@]}"; do
        if is_installed "$pkg"; then
            echo "Package already installed: $pkg"
        else
            MISSING_PACKAGES+=("$pkg")
        fi
    done
}

upgrade_unit_active() {
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl list-units --type=service --state=active,activating --no-legend --plain \
        'pins-sysupgrade-*' 2>/dev/null | grep -q .
}

find_missing_packages
if [[ "${#MISSING_PACKAGES[@]}" -eq 0 ]]; then
    echo "All required PINS packages are already installed."
    exit 0
fi
echo "Missing packages detected: ${MISSING_PACKAGES[*]}"

# Upgrade scripts older than the PINS package lock do not take it, so also wait
# for a detached upgrade unit to end.
waited=0
while upgrade_unit_active; do
    if (( waited >= UPGRADE_WAIT_SECONDS )); then
        echo "A system upgrade is still running after ${waited}s; retrying on the next daemon start." >&2
        exit 1
    fi
    if (( waited == 0 )); then
        echo "Waiting for the running system upgrade to finish..."
    fi
    sleep "$POLL_SECONDS"
    waited=$((waited + POLL_SECONDS))
done

exec 9>"$LOCK_FILE"
if ! flock -w "$LOCK_WAIT_SECONDS" 9; then
    echo "Another PINS package operation is still running; retrying on the next daemon start." >&2
    exit 1
fi

# The upgrade we waited for may have installed them meanwhile.
find_missing_packages
if [[ "${#MISSING_PACKAGES[@]}" -eq 0 ]]; then
    echo "All required PINS packages are already installed."
    exit 0
fi

export DEBIAN_FRONTEND=noninteractive
APT_LOCK_OPTION=(-o DPkg::Lock::Timeout=600)

echo "Updating apt metadata..."
apt-get "${APT_LOCK_OPTION[@]}" update

# One package at a time, so one unavailable package does not block the others.
# --no-remove: never trade an installed package for a missing helper.
FAILED_PACKAGES=()
for pkg in "${MISSING_PACKAGES[@]}"; do
    echo "Installing missing package: $pkg"
    if ! apt-get "${APT_LOCK_OPTION[@]}" install -y --no-remove "$pkg"; then
        FAILED_PACKAGES+=("$pkg")
    fi
done

if [[ "${#FAILED_PACKAGES[@]}" -gt 0 ]]; then
    echo "Could not install required packages: ${FAILED_PACKAGES[*]}" >&2
    exit 1
fi

echo "Required package check completed successfully."
