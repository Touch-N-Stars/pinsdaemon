#!/usr/bin/env python3
import json
import subprocess
import sys
import os
import re
import time
import argparse

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
WIFI_PROFILE_SCRIPT = os.path.join(os.path.dirname(__file__), "pins-wifi-profile.py")
VIABILITY_SCRIPT = os.path.join(os.path.dirname(__file__), "pins-wifi-viability.py")
RETRY_STATE_FILE = "/run/pins-wifi-automanage.last-attempt"
RETRY_SECONDS = 120
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


def run_command(command, **kwargs):
    # wifi-connect.sh validates inherited descriptor 9 against the shared lock.
    if os.name == "posix" and os.environ.get("PINS_WIFI_COORDINATION_LOCK_HELD") == "1":
        kwargs["pass_fds"] = (9,)
    return subprocess.run(command, **kwargs)


def client_is_healthy(interface):
    try:
        probe = run_command([VIABILITY_SCRIPT, "--interface", interface],
                            capture_output=True, text=True, timeout=20)
        return probe.returncode == 0 and "status=healthy" in probe.stdout
    except (OSError, subprocess.TimeoutExpired):
        # Keep the same ordinary-router compatibility path as the watchdog.
        active = run_command(["nmcli", "-t", "-f", "UUID,TYPE,DEVICE", "connection", "show", "--active"],
                             capture_output=True, text=True, timeout=10)
        for line in active.stdout.splitlines():
            fields = line.split(":")
            if len(fields) != 3 or fields[1:] != ["802-11-wireless", interface]:
                continue
            mode = run_command(["nmcli", "-g", "802-11-wireless.mode", "connection", "show", "uuid", fields[0]],
                               capture_output=True, text=True, timeout=10)
            if mode.returncode == 0 and mode.stdout.strip() == "infrastructure":
                route = run_command(["ip", "-4", "route", "show", "default", "dev", interface],
                                    capture_output=True, text=True, timeout=10)
                return route.returncode == 0 and " via " in route.stdout
        return False


def saved_client_networks(interface):
    result = run_command([WIFI_PROFILE_SCRIPT, "--action", "list", "--client-iface", interface],
                         capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("Could not read saved Wi-Fi networks")
    return [n for n in json.loads(result.stdout)["networks"] if n["compatible"] and n["auto_connect"]]


def background_retry_allowed(client, hotspot, now):
    try:
        with open(RETRY_STATE_FILE, encoding="utf-8") as handle:
            previous = float(handle.read())
    except (OSError, ValueError):
        previous = 0
    if 0 <= now - previous < RETRY_SECONDS:
        return False
    if client == hotspot and hotspot_is_ready(hotspot):
        # Single-radio retries interrupt AP access. Fail closed if station
        # inspection is unavailable; explicit UI retries remain possible.
        try:
            stations = run_command(["iw", "dev", hotspot, "station", "dump"],
                                   capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if stations.returncode or stations.stdout.strip():
            return False
    return True


def remember_attempt(now):
    with open(RETRY_STATE_FILE, "w", encoding="utf-8") as handle:
        handle.write(str(now))


def connect_saved_networks(client, hotspot):
    return run_command(wifi_connect_cmd("--saved-networks", "--client-iface", client,
                                       "--hotspot-iface", hotspot)).returncode == 0

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
        run_command(
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

def main(background=False):
    config = load_config() or {}
    client_interface, hotspot_interface = resolve_wifi_interfaces(config)
    
    desired_mode = str(config.get("desired_mode", NETWORK_MODE_AUTO)).strip().lower()
    if desired_mode == NETWORK_MODE_HOTSPOT:
        print("Persistent hotspot mode is selected; skipping client Wi-Fi.")
        start_hotspot(client_interface, hotspot_interface)
        return

    now = time.monotonic()
    if background and not background_retry_allowed(client_interface, hotspot_interface, now):
        return
    if client_is_healthy(client_interface):
        print("Keeping the healthy active client Wi-Fi connection.")
        return
    try:
        networks = saved_client_networks(client_interface)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        print("Saved Wi-Fi networks are unavailable; keeping hotspot fallback.")
        if not background:
            start_hotspot(client_interface, hotspot_interface)
        return
    if not networks and background:
        return
    remember_attempt(now)
    if networks:
        print("Trying saved auto-connect networks in NetworkManager priority order.")
        # One coordinated transition releases the radio, scans, tries candidates,
        # and rolls back to the AP only after all eligible networks fail.
        if connect_saved_networks(client_interface, hotspot_interface):
            return
        print("Saved network connection attempts failed.")

    # Fallback to hotspot
    start_hotspot(client_interface, hotspot_interface)

if __name__ == "__main__":
    import fcntl
    parser = argparse.ArgumentParser()
    parser.add_argument("--background", action="store_true")
    args = parser.parse_args()
    lock_path = "/run/pins-wifi-coordination.lock"
    inherited = False
    if os.environ.get("PINS_WIFI_COORDINATION_LOCK_HELD") == "1":
        try:
            inherited = os.path.samefile("/proc/self/fd/9", lock_path)
        except OSError:
            pass
    try:
        if inherited:
            main(args.background)
        else:
            with open(lock_path, "a") as lock:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if args.background:
                            raise SystemExit(0)
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Another Wi-Fi operation is in progress")
                        time.sleep(0.1)
                os.dup2(lock.fileno(), 9)
                os.environ["PINS_WIFI_COORDINATION_LOCK_HELD"] = "1"
                main(args.background)
    except Exception as exc:
        # Avoid subprocess details/credentials in job logs.
        print(f"Wi-Fi auto-management failed: {type(exc).__name__}")
        raise SystemExit(1)
