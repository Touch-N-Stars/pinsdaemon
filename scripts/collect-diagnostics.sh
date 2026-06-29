#!/bin/bash

set -u

OUTPUT_DIR=""
JOURNAL_LINES=2000
DMESG_LINES=4000
INCLUDE_PINS_JOURNAL=1
INCLUDE_API_JOURNAL=1
INCLUDE_USB=1
INCLUDE_DMESG=1
INCLUDE_SYSTEM_INFO=1
INCLUDE_NETWORK_INFO=1
INCLUDE_KERNEL_MODULES=1
PINSDAEMON_LOG_DIR="${PINSDAEMON_LOG_DIR:-/opt/pinsdaemon/logs}"

usage() {
    echo "Usage: $0 --output-dir <dir> [options]"
    echo "Options:"
    echo "  --journal-lines <n>"
    echo "  --dmesg-lines <n>"
    echo "  --no-pins-journal"
    echo "  --no-api-journal"
    echo "  --no-usb"
    echo "  --no-dmesg"
    echo "  --no-system-info"
    echo "  --no-network-info"
    echo "  --no-kernel-modules"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --output-dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --journal-lines)
            JOURNAL_LINES="$2"
            shift 2
            ;;
        --dmesg-lines)
            DMESG_LINES="$2"
            shift 2
            ;;
        --no-pins-journal)
            INCLUDE_PINS_JOURNAL=0
            shift
            ;;
        --no-api-journal)
            INCLUDE_API_JOURNAL=0
            shift
            ;;
        --no-usb)
            INCLUDE_USB=0
            shift
            ;;
        --no-dmesg)
            INCLUDE_DMESG=0
            shift
            ;;
        --no-system-info)
            INCLUDE_SYSTEM_INFO=0
            shift
            ;;
        --no-network-info)
            INCLUDE_NETWORK_INFO=0
            shift
            ;;
        --no-kernel-modules)
            INCLUDE_KERNEL_MODULES=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [ -z "$OUTPUT_DIR" ]; then
    echo "--output-dir is required" >&2
    usage
    exit 2
fi

mkdir -p "$OUTPUT_DIR"

run_command() {
    local output_file="$1"
    shift

    mkdir -p "$(dirname "$output_file")"

    {
        echo "$ $*"
        "$@"
        local rc=$?
        echo
        echo "[exit-code] $rc"
    } >"$output_file" 2>&1 || true
}

run_shell() {
    local output_file="$1"
    local command_text="$2"

    mkdir -p "$(dirname "$output_file")"

    {
        echo "$ $command_text"
        bash -lc "$command_text"
        local rc=$?
        echo
        echo "[exit-code] $rc"
    } >"$output_file" 2>&1 || true
}

cat >"$OUTPUT_DIR/manifest.txt" <<EOF
collected_at=$(date --iso-8601=seconds)
journal_lines=$JOURNAL_LINES
dmesg_lines=$DMESG_LINES
include_pins_journal=$INCLUDE_PINS_JOURNAL
include_api_journal=$INCLUDE_API_JOURNAL
include_usb=$INCLUDE_USB
include_dmesg=$INCLUDE_DMESG
include_system_info=$INCLUDE_SYSTEM_INFO
include_network_info=$INCLUDE_NETWORK_INFO
include_kernel_modules=$INCLUDE_KERNEL_MODULES
EOF

if [ "$INCLUDE_SYSTEM_INFO" -eq 1 ]; then
    run_command "$OUTPUT_DIR/system/date.txt" date --iso-8601=seconds
    run_command "$OUTPUT_DIR/system/uptime.txt" uptime
    run_command "$OUTPUT_DIR/system/uname.txt" uname -a
    run_command "$OUTPUT_DIR/system/os-release.txt" cat /etc/os-release
    run_command "$OUTPUT_DIR/system/timedatectl.txt" timedatectl status
    run_command "$OUTPUT_DIR/system/systemctl-pins.txt" systemctl status pins --no-pager
    run_command "$OUTPUT_DIR/system/systemctl-pins-service.txt" systemctl status pins.service --no-pager
    run_command "$OUTPUT_DIR/system/systemctl-sysupdate-api.txt" systemctl status sysupdate-api --no-pager
fi

if [ "$INCLUDE_PINS_JOURNAL" -eq 1 ]; then
    run_command "$OUTPUT_DIR/logs/journal-pins.txt" journalctl -u pins -n "$JOURNAL_LINES" --no-pager
    run_command "$OUTPUT_DIR/logs/journal-pins-service.txt" journalctl -u pins.service -n "$JOURNAL_LINES" --no-pager
fi

if [ "$INCLUDE_API_JOURNAL" -eq 1 ]; then
    run_command "$OUTPUT_DIR/logs/journal-sysupdate-api.txt" journalctl -u sysupdate-api -n "$JOURNAL_LINES" --no-pager
    run_command "$OUTPUT_DIR/logs/journal-sysupdate-api-service.txt" journalctl -u sysupdate-api.service -n "$JOURNAL_LINES" --no-pager
    if [ -d "$PINSDAEMON_LOG_DIR" ]; then
        mkdir -p "$OUTPUT_DIR/logs/pinsdaemon-local"
        find "$PINSDAEMON_LOG_DIR" -maxdepth 1 -type f -name '*.log' -mtime -5 -print0 \
            | while IFS= read -r -d '' log_file; do
                cp "$log_file" "$OUTPUT_DIR/logs/pinsdaemon-local/$(basename "$log_file")" 2>/dev/null || true
            done
    fi
fi

if [ "$INCLUDE_USB" -eq 1 ]; then
    run_command "$OUTPUT_DIR/usb/lsusb.txt" lsusb
    run_command "$OUTPUT_DIR/usb/lsusb-tree.txt" lsusb -t
    run_command "$OUTPUT_DIR/usb/usb-devices.txt" usb-devices
fi

if [ "$INCLUDE_DMESG" -eq 1 ]; then
    run_shell "$OUTPUT_DIR/logs/dmesg-tail.txt" "dmesg -T | tail -n $DMESG_LINES"
    run_shell "$OUTPUT_DIR/logs/dmesg-usb.txt" "dmesg -T | grep -Ei 'usb|xhci|uvc|ttyUSB|ttyACM|ftdi|cp210|ch34|hidraw|video' || true"
fi

if [ "$INCLUDE_NETWORK_INFO" -eq 1 ]; then
    run_command "$OUTPUT_DIR/network/nmcli-device-status.txt" nmcli device status
    run_command "$OUTPUT_DIR/network/nmcli-active-connections.txt" nmcli connection show --active
    run_command "$OUTPUT_DIR/network/ip-address.txt" ip address
    run_command "$OUTPUT_DIR/network/ip-route.txt" ip route
    run_command "$OUTPUT_DIR/network/rfkill.txt" rfkill list
    run_command "$OUTPUT_DIR/network/iw-dev.txt" iw dev
fi

if [ "$INCLUDE_KERNEL_MODULES" -eq 1 ]; then
    run_command "$OUTPUT_DIR/system/lsmod.txt" lsmod
fi

echo "Diagnostics collected in $OUTPUT_DIR"
exit 0
