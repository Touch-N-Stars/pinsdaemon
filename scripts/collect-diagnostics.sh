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
COLLECTION_STARTED_EPOCH="$(date +%s)"

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
diagnostics_schema=2
collected_at=$(date --iso-8601=seconds)
boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)
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
    run_command "$OUTPUT_DIR/system/hostnamectl.txt" hostnamectl status
    run_command "$OUTPUT_DIR/system/proc-uptime.txt" cat /proc/uptime
    run_command "$OUTPUT_DIR/system/loadavg.txt" cat /proc/loadavg
    run_command "$OUTPUT_DIR/system/memory.txt" free -h
    run_command "$OUTPUT_DIR/system/filesystems.txt" df -hT
    run_command "$OUTPUT_DIR/system/process-summary.txt" ps -eo pid,ppid,user,stat,etimes,nlwp,%cpu,%mem,comm
    run_command "$OUTPUT_DIR/system/systemctl-failed.txt" systemctl --failed --no-pager
    run_command "$OUTPUT_DIR/system/systemctl-timers.txt" systemctl list-timers --all --no-pager
    run_command "$OUTPUT_DIR/system/systemctl-pins.txt" systemctl status pins --no-pager
    run_command "$OUTPUT_DIR/system/systemctl-pins-service.txt" systemctl status pins.service --no-pager
    run_command "$OUTPUT_DIR/system/systemctl-sysupdate-api.txt" systemctl status sysupdate-api --no-pager
    run_command "$OUTPUT_DIR/system/systemctl-networkmanager.txt" systemctl status NetworkManager.service --no-pager
    run_command "$OUTPUT_DIR/system/systemctl-hotspot-setup.txt" systemctl status hotspot-setup.service --no-pager
    run_command "$OUTPUT_DIR/system/systemctl-wifi-watchdog.txt" systemctl status pins-wifi-watchdog.timer pins-wifi-watchdog.service --no-pager
    run_command "$OUTPUT_DIR/system/systemctl-dnsmasq.txt" systemctl status dnsmasq.service --no-pager
    run_command "$OUTPUT_DIR/system/systemctl-avahi.txt" systemctl status avahi-daemon.service --no-pager
    run_command "$OUTPUT_DIR/system/systemctl-dbus.txt" systemctl status dbus.service --no-pager
    run_command "$OUTPUT_DIR/system/networkmanager-runtime.txt" systemctl show NetworkManager.service -p ActiveState -p SubState -p MainPID -p NRestarts -p TasksCurrent -p MemoryCurrent -p CPUUsageNSec
    run_command "$OUTPUT_DIR/system/dbus-networkmanager.txt" busctl --system status org.freedesktop.NetworkManager
    run_command "$OUTPUT_DIR/system/raspberry-pi-throttling.txt" vcgencmd get_throttled
    run_command "$OUTPUT_DIR/system/raspberry-pi-temperature.txt" vcgencmd measure_temp
    run_command "$OUTPUT_DIR/system/package-versions.txt" dpkg-query -W -f '${binary:Package}\t${Version}\t${db:Status-Abbrev}\n' pinsdaemon network-manager dnsmasq-base wpasupplicant iw iproute2 ethtool avahi-daemon
    run_command "$OUTPUT_DIR/system/networkmanager-version.txt" NetworkManager --version
    run_command "$OUTPUT_DIR/system/nmcli-version.txt" nmcli --version
    run_command "$OUTPUT_DIR/system/dnsmasq-version.txt" dnsmasq --version
    run_command "$OUTPUT_DIR/system/installed-network-script-hashes.txt" sha256sum \
        /usr/local/bin/wifi-connect.sh \
        /usr/local/bin/wifi-automanage.py \
        /usr/local/bin/pins-wifi-watchdog.sh \
        /usr/local/bin/collect-diagnostics.sh \
        /etc/NetworkManager/dispatcher.d/90-pins-wifi-recovery
    run_command "$OUTPUT_DIR/system/pinsdaemon-package-verification.txt" dpkg -V pinsdaemon
fi

if [ "$INCLUDE_PINS_JOURNAL" -eq 1 ]; then
    run_command "$OUTPUT_DIR/logs/journal-pins.txt" journalctl -u pins -n "$JOURNAL_LINES" --no-pager
    run_command "$OUTPUT_DIR/logs/journal-pins-service.txt" journalctl -u pins.service -n "$JOURNAL_LINES" --no-pager
fi

if [ "$INCLUDE_API_JOURNAL" -eq 1 ]; then
    run_command "$OUTPUT_DIR/logs/journal-sysupdate-api.txt" journalctl -u sysupdate-api -n "$JOURNAL_LINES" --no-pager
    run_command "$OUTPUT_DIR/logs/journal-sysupdate-api-service.txt" journalctl -u sysupdate-api.service -n "$JOURNAL_LINES" --no-pager
    run_command "$OUTPUT_DIR/logs/journal-networkmanager.txt" journalctl -u NetworkManager.service -n "$JOURNAL_LINES" --no-pager
    run_command "$OUTPUT_DIR/logs/journal-wifi-recovery.txt" journalctl -t pins-wifi-recovery -n "$JOURNAL_LINES" --no-pager
    run_command "$OUTPUT_DIR/logs/journal-wifi-watchdog.txt" journalctl -t pins-wifi-watchdog -n "$JOURNAL_LINES" --no-pager
    run_command "$OUTPUT_DIR/logs/journal-boots.txt" journalctl --list-boots --no-pager
    run_command "$OUTPUT_DIR/logs/journal-networkmanager-current-boot.txt" journalctl -b -u NetworkManager.service -n "$JOURNAL_LINES" --no-pager
    run_command "$OUTPUT_DIR/logs/journal-networkmanager-current-boot-monotonic.txt" journalctl -b -u NetworkManager.service -o short-monotonic -n "$JOURNAL_LINES" --no-pager
    run_command "$OUTPUT_DIR/logs/journal-networkmanager-previous-boot.txt" journalctl -b -1 -u NetworkManager.service -n "$JOURNAL_LINES" --no-pager
    run_command "$OUTPUT_DIR/logs/journal-avahi-current-boot.txt" journalctl -b -u avahi-daemon.service -n "$JOURNAL_LINES" --no-pager
    run_command "$OUTPUT_DIR/logs/journal-kernel-current-boot.txt" journalctl -b -k -n "$JOURNAL_LINES" --no-pager
    run_command "$OUTPUT_DIR/logs/journal-kernel-previous-boot.txt" journalctl -b -1 -k -n "$JOURNAL_LINES" --no-pager
    run_command "$OUTPUT_DIR/logs/journal-warnings-current-boot.txt" journalctl -b -p warning..alert -n "$JOURNAL_LINES" --no-pager
    run_command "$OUTPUT_DIR/logs/journal-warnings-previous-boot.txt" journalctl -b -1 -p warning..alert -n "$JOURNAL_LINES" --no-pager
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
    run_shell "$OUTPUT_DIR/logs/dmesg-network.txt" "dmesg -T | grep -Ei 'brcmfmac|cfg80211|wlan|wifi|80211|eth0|ethernet|link is|carrier|dhcp|dnsmasq|network' || true"
fi

if [ "$INCLUDE_NETWORK_INFO" -eq 1 ]; then
    run_command "$OUTPUT_DIR/network/nmcli-general-status.txt" nmcli general status
    run_command "$OUTPUT_DIR/network/nmcli-radio.txt" nmcli radio all
    run_command "$OUTPUT_DIR/network/nmcli-connectivity.txt" nmcli networking connectivity
    run_command "$OUTPUT_DIR/network/nmcli-device-status.txt" nmcli device status
    run_command "$OUTPUT_DIR/network/nmcli-device-details.txt" nmcli device show
    run_command "$OUTPUT_DIR/network/nmcli-active-connections.txt" nmcli connection show --active
    run_command "$OUTPUT_DIR/network/nmcli-connections.txt" nmcli -f NAME,UUID,TYPE,DEVICE,AUTOCONNECT,AUTOCONNECT-PRIORITY connection show
    while IFS= read -r profile_uuid; do
        [[ "$profile_uuid" =~ ^[0-9A-Fa-f-]{36}$ ]] || continue
        run_command "$OUTPUT_DIR/network/connections/$profile_uuid.txt" nmcli -f connection.id,connection.uuid,connection.type,connection.interface-name,connection.autoconnect,connection.autoconnect-priority,802-11-wireless.ssid,802-11-wireless.mode,802-11-wireless.band,802-11-wireless.channel,802-11-wireless-security.key-mgmt,ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.route-metric,ipv4.never-default,ipv6.method connection show uuid "$profile_uuid"
    done < <(nmcli -g UUID connection show 2>/dev/null)
    run_command "$OUTPUT_DIR/network/networkmanager-config.txt" NetworkManager --print-config
    run_command "$OUTPUT_DIR/network/ip-address.txt" ip address
    run_command "$OUTPUT_DIR/network/ip-address-v4.txt" ip -4 address show
    run_command "$OUTPUT_DIR/network/ip-address-v6.txt" ip -6 address show
    run_command "$OUTPUT_DIR/network/ip-link-details.txt" ip -details -statistics link show
    run_command "$OUTPUT_DIR/network/ip-route.txt" ip route
    run_command "$OUTPUT_DIR/network/ip-route-all-tables-v4.txt" ip -4 route show table all
    run_command "$OUTPUT_DIR/network/ip-route-all-tables-v6.txt" ip -6 route show table all
    run_command "$OUTPUT_DIR/network/ip-rule-v4.txt" ip -4 rule show
    run_command "$OUTPUT_DIR/network/ip-rule-v6.txt" ip -6 rule show
    run_command "$OUTPUT_DIR/network/ip-neighbors.txt" ip neighbor show
    run_command "$OUTPUT_DIR/network/listening-sockets.txt" ss -lntup
    run_command "$OUTPUT_DIR/network/socket-summary.txt" ss -s
    run_command "$OUTPUT_DIR/network/rfkill.txt" rfkill list
    run_command "$OUTPUT_DIR/network/iw-dev.txt" iw dev
    run_command "$OUTPUT_DIR/network/iw-regulatory-domain.txt" iw reg get
    run_command "$OUTPUT_DIR/network/iw-capabilities.txt" iw list
    run_shell "$OUTPUT_DIR/network/resolver-status.txt" "if command -v resolvectl >/dev/null 2>&1; then resolvectl status; elif command -v systemd-resolve >/dev/null 2>&1; then systemd-resolve --status; else echo 'resolver status command unavailable'; fi"
    run_command "$OUTPUT_DIR/network/resolv-conf.txt" cat /etc/resolv.conf
    run_command "$OUTPUT_DIR/network/firewall-nft.txt" nft list ruleset
    run_shell "$OUTPUT_DIR/network/firewall-iptables-v4.txt" "if command -v iptables-save >/dev/null 2>&1; then iptables-save; else echo 'iptables-save unavailable'; fi"
    run_shell "$OUTPUT_DIR/network/firewall-iptables-v6.txt" "if command -v ip6tables-save >/dev/null 2>&1; then ip6tables-save; else echo 'ip6tables-save unavailable'; fi"
    run_command "$OUTPUT_DIR/network/sysctl-forwarding.txt" sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding
    run_command "$OUTPUT_DIR/network/proc-net-dev.txt" cat /proc/net/dev
    run_command "$OUTPUT_DIR/network/proc-net-softnet-stat.txt" cat /proc/net/softnet_stat
    run_command "$OUTPUT_DIR/network/coordination-locks.txt" lslocks
    run_shell "$OUTPUT_DIR/network/recovery-state.txt" "for file in /run/pins-wifi*.failures; do [ -e \"\$file\" ] || continue; printf '%s=' \"\$file\"; cat \"\$file\"; done"
    run_command "$OUTPUT_DIR/network/dnsmasq-shared-config.txt" cat /etc/NetworkManager/dnsmasq-shared.d/pins-local-only.conf

    for interface_path in /sys/class/net/*; do
        [ -e "$interface_path" ] || continue
        interface="$(basename "$interface_path")"
        if ! [[ "$interface" =~ ^[A-Za-z0-9_.:-]+$ ]]; then
            continue
        fi
        run_command "$OUTPUT_DIR/network/interfaces/$interface-ethtool.txt" ethtool "$interface"
        run_command "$OUTPUT_DIR/network/interfaces/$interface-driver.txt" ethtool -i "$interface"
        run_command "$OUTPUT_DIR/network/interfaces/$interface-statistics.txt" ethtool -S "$interface"
        run_command "$OUTPUT_DIR/network/interfaces/$interface-iw-link.txt" iw dev "$interface" link
        run_command "$OUTPUT_DIR/network/interfaces/$interface-udev.txt" udevadm info --query=property --path "$interface_path"
        run_shell "$OUTPUT_DIR/network/interfaces/$interface-sysfs.txt" "for field in address carrier carrier_changes duplex flags ifindex mtu operstate speed type; do printf '%s=' \"\$field\"; cat /sys/class/net/$interface/\"\$field\" 2>/dev/null || echo unavailable; done; printf 'driver='; readlink -f /sys/class/net/$interface/device/driver 2>/dev/null || echo unavailable"
    done

    if [ -f /opt/pinsdaemon/app/wifi_config.json ]; then
        run_command "$OUTPUT_DIR/network/wifi-config.txt" cat /opt/pinsdaemon/app/wifi_config.json
    fi
fi

if [ "$INCLUDE_KERNEL_MODULES" -eq 1 ]; then
    run_command "$OUTPUT_DIR/system/lsmod.txt" lsmod
fi

COLLECTION_FINISHED_EPOCH="$(date +%s)"
cat >>"$OUTPUT_DIR/manifest.txt" <<EOF
completed_at=$(date --iso-8601=seconds)
duration_seconds=$((COLLECTION_FINISHED_EPOCH - COLLECTION_STARTED_EPOCH))
EOF

echo "Diagnostics collected in $OUTPUT_DIR"
exit 0
