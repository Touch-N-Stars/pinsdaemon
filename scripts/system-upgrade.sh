#!/usr/bin/env bash
set -e

echo "Starting system upgrade..."
ORIGINAL_ARGS=("$@")

TARGET_KERNEL_VERSION="${PINS_TARGET_KERNEL_VERSION:-6.12.75-v8-16k+}"
TARGET_RPI_UPDATE_HASH="${PINS_TARGET_RPI_UPDATE_HASH:-98655d3ccedba33aeadd0e550229f1496c5bf6f9}"
KERNEL_PACKAGE_CANDIDATES=(
    "raspberrypi-kernel"
    "raspberrypi-bootloader"
    "linux-image-rpi-v8"
    "linux-image-rpi-v8-16k"
    "linux-headers-rpi-v8"
    "linux-headers-rpi-v8-16k"
)

# Default variables
DRY_RUN=false
JOB_ID=""
STATE_FILE=""
HANDOFF_TO_DETACHED=false
STARTED_AT="$(date +%s)"

write_job_state() {
    local status="$1"
    local exit_code="$2"
    local finished_at="$3"

    if [[ -z "$JOB_ID" || -z "$STATE_FILE" ]]; then
        return 0
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        return 0
    fi

    if ! python3 - "$STATE_FILE" "$JOB_ID" "$status" "$exit_code" "$STARTED_AT" "$finished_at" "$0" "$DRY_RUN" <<'PY'
import json
import os
import sys

state_file = sys.argv[1]
job_id = sys.argv[2]
status = sys.argv[3]
exit_code_raw = sys.argv[4]
started_at_raw = sys.argv[5]
finished_at_raw = sys.argv[6]
script_path = sys.argv[7]
dry_run_raw = sys.argv[8]

existing = {}
try:
    with open(state_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        existing = data
except Exception:
    existing = {}

started_at = existing.get("startedAt")
if started_at is None:
    try:
        started_at = float(started_at_raw)
    except ValueError:
        started_at = None

try:
    finished_at = float(finished_at_raw) if finished_at_raw else None
except ValueError:
    finished_at = None

try:
    exit_code = int(exit_code_raw) if exit_code_raw != "" else None
except ValueError:
    exit_code = None

command = f"sudo -n {script_path} --job-id {job_id}"
if dry_run_raw.lower() == "true":
    command += " --dry-run"

payload = {
    "jobId": job_id,
    "status": status,
    "exitCode": exit_code,
    "startedAt": started_at,
    "finishedAt": finished_at,
    "command": command,
}

with open(state_file, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2)
    f.write("\n")
PY
    then
        return 0
    fi

    if id "sysupdate-api" >/dev/null 2>&1; then
        chown sysupdate-api:sysupdate-api "$STATE_FILE" 2>/dev/null || true
    fi
    chmod 664 "$STATE_FILE" 2>/dev/null || true
}

finalize_job_state() {
    local exit_code=$?

    if [[ "$HANDOFF_TO_DETACHED" == "true" ]]; then
        return "$exit_code"
    fi

    if [[ "$exit_code" -eq 0 ]]; then
        write_job_state "success" "0" "$(date +%s)" || true
    else
        write_job_state "failed" "$exit_code" "$(date +%s)" || true
    fi

    return "$exit_code"
}

trap finalize_job_state EXIT

is_package_installed() {
    local package_name="$1"
    dpkg-query -W -f='${Status}' "$package_name" 2>/dev/null | grep -q "install ok installed"
}

hold_kernel_packages() {
    local held_any=false
    local package_name

    for package_name in "${KERNEL_PACKAGE_CANDIDATES[@]}"; do
        if is_package_installed "$package_name"; then
            echo "Holding kernel package: $package_name"
            apt-mark hold "$package_name" >/dev/null
            held_any=true
        fi
    done

    if [[ "$held_any" != "true" ]]; then
        echo "No known kernel package candidates installed; skipping apt hold step."
    fi
}

enforce_target_kernel_version() {
    local running_kernel
    running_kernel="$(uname -r 2>/dev/null || true)"

    echo "Running kernel: ${running_kernel:-unknown}"
    echo "Target kernel:  $TARGET_KERNEL_VERSION"

    if [[ "$running_kernel" == "$TARGET_KERNEL_VERSION" ]]; then
        echo "Kernel already matches target version."
        return 0
    fi

    echo "Kernel mismatch detected. Enforcing pinned kernel with rpi-update $TARGET_RPI_UPDATE_HASH"

    if ! command -v rpi-update >/dev/null 2>&1; then
        echo "rpi-update not found. Installing..."
        stdbuf -oL -eL apt-get install -y rpi-update
    fi

    SKIP_WARNING=1 stdbuf -oL -eL rpi-update "$TARGET_RPI_UPDATE_HASH"
    echo "Pinned kernel files applied. Reboot required for kernel change to take effect."
}

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --dry-run) DRY_RUN=true ;;
        --job-id) JOB_ID="$2"; shift ;;
        --state-file) STATE_FILE="$2"; shift ;;
        *) echo "Unknown parameter: $1"; exit 1 ;;
    esac
    shift
done

write_job_state "running" "" "" || true

# Optional: Handle dry-run argument
if [[ "$DRY_RUN" == "true" ]]; then
    echo "Dry run mode active. No changes will be made."
    echo "Files needed to be upgraded would be listed here."
    # Simulate work
    sleep 2
    echo "Done (Dry Run)."
    exit 0
fi

# Detach the upgrade process to prevent interruption when the service restarts
if [[ "${PINS_UPDATE_DETACHED}" != "true" ]]; then
    echo "Checking for systemd-run to detach process..."
    if command -v systemd-run >/dev/null 2>&1; then
        echo "Detaching upgrade process via systemd-run..."
        # Use systemd-run to start this script in a new transient unit
        # This prevents the script from being killed when sysupdate-api service stops
        
        # Build unit name from JOB_ID if available, else date
        if [[ -n "$JOB_ID" ]]; then
            UNIT_NAME="pins-sysupgrade-${JOB_ID}"
        else
            UNIT_NAME="pins-sysupgrade-$(date +%s)"
        fi
        
        # Explicitly echo the unit name so the backend can parse it reliably.
        echo "Running as unit: ${UNIT_NAME}.service"

        # Suppress systemd-run's own output to avoid duplicate parsing or confusion, 
        # but capture potential errors.
        if ! OUTPUT=$(systemd-run --unit="${UNIT_NAME}" \
                    --setenv=PINS_UPDATE_DETACHED=true \
                    --no-block \
                    "$0" "${ORIGINAL_ARGS[@]}" 2>&1); then
            echo "Failed to start systemd-run: $OUTPUT"
            exit 1
        fi
        
        HANDOFF_TO_DETACHED=true
        echo "Upgrade process detached and started in background."
        echo "The system may update and restart the pinsdaemon service shortly."
        exit 0
    else
        echo "Warning: systemd-run not found. Proceeding in foreground."
    fi
fi

# Update package lists
echo "Running apt update..."
export DEBIAN_FRONTEND=noninteractive

echo "Holding kernel-related packages to avoid unintended kernel upgrades..."
hold_kernel_packages

# frequent flush for logs
stdbuf -oL -eL apt-get update

# Upgrade packages
echo "Running apt upgrade..."
UPGRADE_OUTPUT=$(stdbuf -oL -eL apt-get upgrade -y 2>&1)
echo "$UPGRADE_OUTPUT"

HAS_PACKAGE_UPDATES=true

# Provide a clear signal for clients when no upgrades are available.
if echo "$UPGRADE_OUTPUT" | grep -qE '^0 upgraded, 0 newly installed, 0 to remove'; then
    HAS_PACKAGE_UPDATES=false
    echo "System is already up to date."
fi

echo "Cleaning APT cache..."
stdbuf -oL -eL apt-get clean
stdbuf -oL -eL apt-get autoclean

echo "Validating pinned kernel version..."
enforce_target_kernel_version

if [[ "$HAS_PACKAGE_UPDATES" == "true" ]]; then
    echo "Updates detected. Restarting pins service..."
    systemctl restart pins
else
    echo "No package updates detected. Skipping pins restart."
fi

echo "System upgrade completed successfully."
