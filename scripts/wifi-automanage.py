#!/usr/bin/env python3
import json
import subprocess
import sys
import os
import time
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

def scan_networks(ssid, interface):
    try:
        # Force a scan
        subprocess.run(["nmcli", "device", "wifi", "rescan", "ifname", interface], check=False)
        time.sleep(3)
        
        # List networks
        result = subprocess.run(
            ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list"],
            capture_output=True,
            text=True,
            check=True
        )
        available_ssids = result.stdout.strip().split('\n')
        return ssid in available_ssids
    except subprocess.CalledProcessError as e:
        print(f"Error scanning networks: {e}")
        return False

def connect_to_wifi(ssid, band=None, client_interface=DEFAULT_WIFI_INTERFACE, hotspot_interface=DEFAULT_WIFI_INTERFACE):
    print(f"Attempting to connect to {ssid} (Band: {band}, client={client_interface}, hotspot={hotspot_interface})...")
    try:
        args = wifi_connect_cmd(
            "--client-iface", client_interface,
            "--hotspot-iface", hotspot_interface,
            ssid,
            "",
            band if band else "",
        )

        result = subprocess.run(args)
        return result.returncode == 0
            
    except Exception as e:
        print(f"Exception during connection: {e}")
        return False

def start_hotspot(client_interface=DEFAULT_WIFI_INTERFACE, hotspot_interface=DEFAULT_WIFI_INTERFACE):
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
    client_interface, hotspot_interface = get_configured_interfaces(config)
    
    if not config:
        print("No wifi configuration found.")
        start_hotspot(client_interface, hotspot_interface)
        return

    ssid = config.get("ssid")
    auto_connect = config.get("auto_connect", False)
    band = config.get("band", None) # "bg" or "a"

    if auto_connect and ssid:
        print(f"Auto-connect enabled for SSID: {ssid}")
        if scan_networks(ssid, client_interface):
            print(f"Network {ssid} found.")
            if connect_to_wifi(ssid, band, client_interface, hotspot_interface):
                 sys.exit(0)
            else:
                 print("Connection failed.")
        else:
            print(f"Network {ssid} not found in scan results.")
    else:
        print("Auto-connect disabled or SSID not configured.")

    # Fallback to hotspot
    start_hotspot(client_interface, hotspot_interface)

if __name__ == "__main__":
    main()
