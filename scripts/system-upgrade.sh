#!/usr/bin/env bash
set -e

echo "Starting system upgrade..."
ORIGINAL_ARGS=("$@")

# Default variables
DRY_RUN=false
JOB_ID=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --dry-run) DRY_RUN=true ;;
        --job-id) JOB_ID="$2"; shift ;;
        *) echo "Unknown parameter: $1"; exit 1 ;;
    esac
    shift
done

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
# frequent flush for logs
stdbuf -oL -eL apt-get update

# Upgrade packages
echo "Running apt upgrade..."
UPGRADE_OUTPUT=$(stdbuf -oL -eL apt-get upgrade -y 2>&1)
echo "$UPGRADE_OUTPUT"

# Provide a clear signal for clients when no upgrades are available.
if echo "$UPGRADE_OUTPUT" | grep -qE '^0 upgraded, 0 newly installed, 0 to remove'; then
    echo "System is already up to date."
fi

echo "System upgrade completed successfully."
