import json
import os
from typing import Optional, Dict, Any

# In production this might be /etc/pins/wifi.json or similar
# For now, we'll keep it in the app directory or relative to it.
# Let's say we store it in the same directory as this file for simplicity, 
# but in production it should be somewhere persistent.
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "wifi_config.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "ssid": None,
    "auto_connect": False,
    "band": None,
    "client_interface": "wlan0",
    "hotspot_interface": "wlan0",
}


def _merged_config(data: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    return merged

def load_wifi_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_FILE):
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return dict(DEFAULT_CONFIG)
            return _merged_config(data)
    except Exception:
        return dict(DEFAULT_CONFIG)

def save_wifi_config(
    ssid: Optional[str],
    auto_connect: bool,
    band: Optional[str] = None,
    client_interface: Optional[str] = None,
    hotspot_interface: Optional[str] = None,
):
    current = load_wifi_config()
    config = {
        "ssid": ssid,
        "auto_connect": auto_connect,
        "band": band,
        "client_interface": client_interface if client_interface is not None else current.get("client_interface", "wlan0"),
        "hotspot_interface": hotspot_interface if hotspot_interface is not None else current.get("hotspot_interface", "wlan0"),
    }
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4)
