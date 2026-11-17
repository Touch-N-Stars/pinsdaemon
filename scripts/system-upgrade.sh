#!/usr/bin/env bash
set -e

echo "Starting system upgrade..."
ORIGINAL_ARGS=("$@")

TARGET_KERNEL_VERSION="${PINS_TARGET_KERNEL_VERSION:-6.18.50}"
TARGET_RPI_UPDATE_HASH="${PINS_TARGET_RPI_UPDATE_HASH:-e3f5c8e74ebef74ebd8c094576d50c3e55abedce}"
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
# Raw APT state of the last run, kept for diagnostics until the next upgrade.
UPGRADE_WORK_DIR="${PINS_UPGRADE_WORK_DIR:-/var/log/pins-upgrade}"
REPORT_FILE=""
REPORT_HELPER="$(dirname "$0")/pins-upgrade-report.py"
PINS_REPOSITORY_LOCK="${PINS_REPOSITORY_LOCK:-/run/lock/pins-repository.lock}"

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

    if ! python3 - "$STATE_FILE" "$JOB_ID" "$status" "$exit_code" "$STARTED_AT" "$finished_at" "$0" "$DRY_RUN" "$REPORT_FILE" <<'PY'
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
report_file = sys.argv[9]

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

# The structured outcome (see pins-upgrade-report.py) is only attached to the
# final record, so clients never see a stale report on a running job.
if status in ("success", "failed") and report_file:
    try:
        with open(report_file, "r", encoding="utf-8") as f:
            report = json.load(f)
    except Exception:
        report = None
    if isinstance(report, dict):
        payload["upgradeReport"] = report
        if status == "failed" and report.get("errorCode"):
            payload["errorCode"] = report.get("errorCode")
            payload["errorMessage"] = report.get("message")

# Write atomically: pinsdaemon may read the file at any moment, and a torn read
# makes the job look unknown (404) to clients.
temp_file = f"{state_file}.{os.getpid()}.tmp"
with open(temp_file, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2)
    f.write("\n")
    f.flush()
    os.fsync(f.fileno())
os.replace(temp_file, state_file)
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

    # Raspberry Pi kernel release strings append platform/build suffixes, for
    # example 6.18.39+rpt-rpi-v8. Accept the approved base release and all of
    # its normal dot/dash/plus suffixed variants as the target version.
    if [[ "$running_kernel" == "$TARGET_KERNEL_VERSION" \
        || "$running_kernel" == "$TARGET_KERNEL_VERSION"[-.+]* ]]; then
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


exec 9>"$PINS_REPOSITORY_LOCK"
flock -n 9 || { echo "Another PINS package operation is running" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
# APT output is parsed below; keep it in a fixed language.
export LC_ALL=C LANG=C

# Never full-upgrade/dist-upgrade: `upgrade` cannot remove packages, and
# --with-new-pkgs only lets it install dependencies that newer versions add
# (e.g. avahi-utils for pinsdaemon) instead of keeping those packages back.
APT_UPGRADE_FLAGS=(--with-new-pkgs --allow-downgrades)
APT_LOCK_OPTION=(-o DPkg::Lock::Timeout=300)

rm -rf "$UPGRADE_WORK_DIR"
mkdir -p "$UPGRADE_WORK_DIR"
chmod 755 "$UPGRADE_WORK_DIR"
REPORT_FILE="$UPGRADE_WORK_DIR/report.json"

run_helper() {
    python3 "$REPORT_HELPER" "$@"
}

# Simulates an apt-get command into <name>.txt/<name>.rc; never fails the script.
simulate() {
    local name="$1"
    shift
    local rc=0
    apt-get -s "$@" >"$UPGRADE_WORK_DIR/$name.txt" 2>&1 || rc=$?
    echo "$rc" >"$UPGRADE_WORK_DIR/$name.rc"
    return 0
}

collect_package_state() {
    local pins_packages=()
    apt-mark showhold >"$UPGRADE_WORK_DIR/held.txt" 2>/dev/null || true
    dpkg-query -W -f='${Package}\t${Version}\t${db:Status-Abbrev}\n' \
        >"$UPGRADE_WORK_DIR/installed.txt" 2>/dev/null || true
    mapfile -t pins_packages < <(run_helper pins-installed "$UPGRADE_WORK_DIR/installed.txt")
    if [[ "${#pins_packages[@]}" -gt 0 ]]; then
        apt-cache policy "${pins_packages[@]}" >"$UPGRADE_WORK_DIR/policy.txt" 2>&1 || true
    else
        : >"$UPGRADE_WORK_DIR/policy.txt"
    fi
}

echo "Holding kernel-related packages to avoid unintended kernel upgrades..."
hold_kernel_packages

# A previous run that was interrupted (power loss, killed unit) leaves dpkg with
# half-configured packages; finishing their configuration is the safe recovery.
if [[ -n "$(dpkg --audit 2>/dev/null)" ]]; then
    echo "Interrupted package installation detected; completing it with dpkg --configure -a..."
    stdbuf -oL -eL dpkg --configure -a || echo "dpkg --configure -a failed; the upgrade report will include the remaining problem."
fi

echo "Running apt update..."
stdbuf -oL -eL apt-get "${APT_LOCK_OPTION[@]}" update

echo "Planning upgrade..."
simulate plan upgrade "${APT_UPGRADE_FLAGS[@]}"
PLAN_RC=0
run_helper check-plan "$UPGRADE_WORK_DIR/plan.txt" --rc "$(cat "$UPGRADE_WORK_DIR/plan.rc")" \
    --report "$REPORT_FILE" --protected "${KERNEL_PACKAGE_CANDIDATES[@]}" || PLAN_RC=$?
if [[ "$PLAN_RC" -ne 0 ]]; then
    cat "$UPGRADE_WORK_DIR/plan.txt"
    echo "PINS_UPGRADE_RESULT outcome=blocked code=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["errorCode"])' "$REPORT_FILE" 2>/dev/null || echo UNKNOWN)"
    echo "Upgrade stopped before changing any package."
    exit "$PLAN_RC"
fi

echo "Running apt upgrade..."
set +e
stdbuf -oL -eL apt-get "${APT_LOCK_OPTION[@]}" upgrade -y "${APT_UPGRADE_FLAGS[@]}" --no-remove 2>&1 \
    | tee "$UPGRADE_WORK_DIR/upgrade.log"
UPGRADE_RC="${PIPESTATUS[0]}"
set -e
echo "$UPGRADE_RC" >"$UPGRADE_WORK_DIR/upgrade.rc"

TARGETED_INSTALLED=false
if [[ "$UPGRADE_RC" -eq 0 ]]; then
    # The PINS packages must not be left behind silently. If the upgrade kept one
    # back, try exactly those packages once, only if APT can do it without
    # removals or kernel changes.
    collect_package_state
    mapfile -t PENDING_PINS < <(run_helper pins-pending "$UPGRADE_WORK_DIR/policy.txt")
    if [[ "${#PENDING_PINS[@]}" -gt 0 ]]; then
        echo "PINS packages still pending after the upgrade: ${PENDING_PINS[*]}"
        simulate targeted-plan install --only-upgrade --no-remove --allow-downgrades "${PENDING_PINS[@]}"
        TARGETED_CHECK_RC=0
        run_helper check-plan "$UPGRADE_WORK_DIR/targeted-plan.txt" \
            --rc "$(cat "$UPGRADE_WORK_DIR/targeted-plan.rc")" \
            --protected "${KERNEL_PACKAGE_CANDIDATES[@]}" \
            >"$UPGRADE_WORK_DIR/targeted-check.txt" || TARGETED_CHECK_RC=$?
        if [[ "$TARGETED_CHECK_RC" -eq 0 ]]; then
            echo "Installing pending PINS packages explicitly..."
            set +e
            stdbuf -oL -eL apt-get "${APT_LOCK_OPTION[@]}" install -y --only-upgrade --no-remove \
                --allow-downgrades "${PENDING_PINS[@]}" 2>&1 | tee "$UPGRADE_WORK_DIR/targeted.log"
            TARGETED_RC="${PIPESTATUS[0]}"
            set -e
            echo "$TARGETED_RC" >"$UPGRADE_WORK_DIR/targeted.rc"
            [[ "$TARGETED_RC" -eq 0 ]] && TARGETED_INSTALLED=true
        else
            echo "Pending PINS packages cannot be installed safely: $(cat "$UPGRADE_WORK_DIR/targeted-check.txt")"
            cat "$UPGRADE_WORK_DIR/targeted-plan.txt"
        fi
    fi
fi

echo "Verifying upgrade result..."
dpkg --audit >"$UPGRADE_WORK_DIR/dpkg-audit.txt" 2>&1 || true
collect_package_state
simulate verify upgrade "${APT_UPGRADE_FLAGS[@]}"
RESULT_RC=0
run_helper evaluate "$UPGRADE_WORK_DIR" --protected "${KERNEL_PACKAGE_CANDIDATES[@]}" || RESULT_RC=$?

if [[ "$UPGRADE_RC" -ne 0 ]]; then
    exit "$RESULT_RC"
fi

echo "Cleaning APT cache..."
stdbuf -oL -eL apt-get clean || true
stdbuf -oL -eL apt-get autoclean || true

# A wayvnc upgrade can replace the control service with one that drops
# unauthenticated clients; adapt the VNC config in the same run.
WAYVNC_AUTH_SCRIPT="/usr/local/bin/ensure-wayvnc-auth.sh"
if [[ -x "$WAYVNC_AUTH_SCRIPT" ]]; then
    echo "Checking WayVNC authentication..."
    "$WAYVNC_AUTH_SCRIPT" || echo "WayVNC authentication check failed; continuing."
fi

echo "Validating pinned kernel version..."
enforce_target_kernel_version

if grep -q '^Inst ' "$UPGRADE_WORK_DIR/plan.txt" || [[ "$TARGETED_INSTALLED" == "true" ]]; then
    echo "Updates installed. Restarting pins service..."
    systemctl restart pins || echo "Restarting pins failed; continuing."
else
    echo "No package updates installed. Skipping pins restart."
fi

if [[ "$RESULT_RC" -ne 0 ]]; then
    echo "System upgrade did not complete; see the report above."
    exit "$RESULT_RC"
fi
# Only a complete run claims completion; a run with kept-back packages says so.
if grep -q '"outcome": "partial"' "$REPORT_FILE"; then
    echo "System upgrade finished with packages kept back."
else
    echo "System upgrade completed successfully."
fi
