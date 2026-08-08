#!/usr/bin/env python3
import json
import subprocess
import sys
import os
import re

# Configuration paths
# Assuming this script is in /usr/local/bin or similar in production,
# but for now we look for config relative to the app structure or in /etc/pins
CONFIG_PATHS = [
    "/opt/pinsdaemon/app/wifi_config.json",
    "/etc/pins/wifi_config.json",
    os.path.join(os.path.dirname(__file__), "../app/wifi_config.json"), # For development
    "wifi_config.json"
]

WIFI_CONNECT_SCRIPT = os.path.join(os.path.dirname(__file__), "wifi-connect.sh")
DEFAULT_WIFI_INTERFACE = "wlan0"
NETWORK_MODE_AUTO = "auto"
NETWORK_MODE_HOTSPOT = "hotspot"
_IFACE_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _sanitize_interface(value, fallback):
    if not isinstance(value, str):
        return fallback
    candidate = value.strip()
    if not candidate or not _IFACE_RE.fullmatch(candidate):
        return fallback
    return candidate


def get_configured_interfaces(config):
    if not isinstance(config, dict):
        return DEFAULT_WIFI_INTERFACE, DEFAULT_WIFI_INTERFACE
    client_iface = _sanitize_interface(config.get("client_interface"), DEFAULT_WIFI_INTERFACE)
    hotspot_iface = _sanitize_interface(config.get("hotspot_interface"), client_iface)
    return client_iface, hotspot_iface


def list_wifi_interfaces():
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Could not enumerate Wi-Fi adapters: {exc}")
        return []

    interfaces = []
    for line in result.stdout.splitlines():
        device, separator, device_type = line.partition(":")
        if separator and device and device_type == "wifi":
            interfaces.append(device)
    return interfaces


def resolve_wifi_interfaces(config):
    configured_client, configured_hotspot = get_configured_interfaces(config)
    available = list_wifi_interfaces()
    if not available:
        # Keep the historic fallback so wifi-connect.sh can provide its own
        # diagnostic if NetworkManager temporarily cannot enumerate adapters.
        return configured_client, configured_hotspot

    default_interface = DEFAULT_WIFI_INTERFACE if DEFAULT_WIFI_INTERFACE in available else available[0]
    client_iface = configured_client if configured_client in available else default_interface
    hotspot_iface = configured_hotspot if configured_hotspot in available else client_iface
    if hotspot_iface == client_iface and len(available) > 1:
        hotspot_iface = next(interface for interface in available if interface != client_iface)
    if (client_iface, hotspot_iface) != (configured_client, configured_hotspot):
        print(
            "Configured Wi-Fi adapter is unavailable; using "
            f"client={client_iface}, hotspot={hotspot_iface}."
        )
    return client_iface, hotspot_iface


def wifi_connect_cmd(*args):
    if os.geteuid() == 0:
        return [WIFI_CONNECT_SCRIPT, *args]
    return ["sudo", "-n", WIFI_CONNECT_SCRIPT, *args]

def load_config():
    for path in CONFIG_PATHS:
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading config from {path}: {e}")
    return None

def normalize_band(band):
    if not isinstance(band, str):
        return ""
    candidate = band.strip().lower()
    if candidate in {"2.4ghz", "bg"}:
        return "bg"
    if candidate in {"5ghz", "a"}:
        return "a"
    return ""

def connect_to_wifi(ssid, band=None, client_interface=DEFAULT_WIFI_INTERFACE, hotspot_interface=DEFAULT_WIFI_INTERFACE):
    print(f"Attempting to connect to {ssid} (Band: {band}, client={client_interface}, hotspot={hotspot_interface})...")
    try:
        normalized_band = normalize_band(band)
        args = wifi_connect_cmd(
            "--password-stdin",
            "--client-iface", client_interface,
            "--hotspot-iface", hotspot_interface,
            "--auto-connect", "yes",
            "--band", normalized_band,
            ssid,
        )

        result = subprocess.run(args, input="\n", text=True)
        return result.returncode == 0
            
    except Exception as e:
        print(f"Exception during connection: {e}")
        return False

def hotspot_is_ready(interface):
    try:
        active = subprocess.run(
            ["nmcli", "-t", "-f", "UUID,TYPE,DEVICE", "connection", "show", "--active"],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in active.stdout.splitlines():
            fields = line.split(":", 2)
            if len(fields) != 3 or fields[1] != "802-11-wireless" or fields[2] != interface:
                continue
            mode = subprocess.run(
                ["nmcli", "-g", "802-11-wireless.mode", "connection", "show", "uuid", fields[0]],
                capture_output=True,
                text=True,
                check=False,
            )
            if mode.returncode != 0 or mode.stdout.strip() != "ap":
                continue
            addresses = subprocess.run(
                ["nmcli", "-g", "IP4.ADDRESS", "device", "show", interface],
                capture_output=True,
                text=True,
                check=False,
            )
            return addresses.returncode == 0 and any(
                address.strip().startswith("10.42.0.1/")
                for address in addresses.stdout.splitlines()
            )
    except (OSError, subprocess.CalledProcessError):
        return False
    return False

def start_hotspot(client_interface=DEFAULT_WIFI_INTERFACE, hotspot_interface=DEFAULT_WIFI_INTERFACE):
    if hotspot_is_ready(hotspot_interface):
        print(f"Fallback hotspot is already active on {hotspot_interface}.")
        return
    print(f"Starting hotspot on {hotspot_interface} (client iface: {client_interface})...")
    try:
        subprocess.run(
            wifi_connect_cmd(
                "--hotspot",
                "--client-iface", client_interface,
                "--hotspot-iface", hotspot_interface,
            ),
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Failed to start hotspot: {e}")
        sys.exit(1)

def main():
    config = load_config()
    client_interface, hotspot_interface = resolve_wifi_interfaces(config)
    
    if not config:
        print("No wifi configuration found.")
        start_hotspot(client_interface, hotspot_interface)
        return

    desired_mode = str(config.get("desired_mode", NETWORK_MODE_AUTO)).strip().lower()
    if desired_mode == NETWORK_MODE_HOTSPOT:
        print("Persistent hotspot mode is selected; skipping client Wi-Fi.")
        start_hotspot(client_interface, hotspot_interface)
        return

    ssid = config.get("ssid")
    auto_connect = config.get("auto_connect", False)
    band = config.get("band", None) # "bg" or "a"

    if auto_connect and ssid:
        print(f"Auto-connect enabled for SSID: {ssid}")
        # wifi-connect.sh owns the complete radio transition. On a single
        # adapter it must stop the AP before NetworkManager can scan or
        # activate the saved client profile. A pre-scan here runs while the
        # radio is still in AP mode and incorrectly reports the WLAN missing.
        if connect_to_wifi(ssid, band, client_interface, hotspot_interface):
            sys.exit(0)
        else:
            print("Connection failed.")
    else:
        print("Auto-connect disabled or SSID not configured.")

    # Fallback to hotspot
    start_hotspot(client_interface, hotspot_interface)

if __name__ == "__main__":
    main()
