import json
import os
import tempfile
from typing import Optional, Dict, Any

# In production this might be /etc/pins/wifi.json or similar
# For now, we'll keep it in the app directory or relative to it.
# Let's say we store it in the same directory as this file for simplicity, 
# but in production it should be somewhere persistent.
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "wifi_config.json")
NETWORK_MODE_AUTO = "auto"
NETWORK_MODE_HOTSPOT = "hotspot"
VALID_NETWORK_MODES = {NETWORK_MODE_AUTO, NETWORK_MODE_HOTSPOT}

DEFAULT_CONFIG: Dict[str, Any] = {
    "ssid": None,
    "auto_connect": False,
    "band": None,
    "client_interface": "wlan0",
    "hotspot_interface": "wlan0",
    "desired_mode": NETWORK_MODE_AUTO,
}


def normalize_network_mode(value: Any) -> str:
    if not isinstance(value, str):
        return NETWORK_MODE_AUTO
    candidate = value.strip().lower()
    if candidate not in VALID_NETWORK_MODES:
        return NETWORK_MODE_AUTO
    return candidate


def _merged_config(data: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    merged["desired_mode"] = normalize_network_mode(merged.get("desired_mode"))
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


def _atomic_write_config(config: Dict[str, Any]) -> None:
    config_dir = os.path.dirname(CONFIG_FILE)
    os.makedirs(config_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".wifi_config.", suffix=".tmp", dir=config_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, CONFIG_FILE)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def save_wifi_config(
    ssid: Optional[str],
    auto_connect: bool,
    band: Optional[str] = None,
    client_interface: Optional[str] = None,
    hotspot_interface: Optional[str] = None,
    desired_mode: Optional[str] = None,
):
    current = load_wifi_config()
    config = {
        "ssid": ssid,
        "auto_connect": auto_connect,
        "band": band,
        "client_interface": client_interface if client_interface is not None else current.get("client_interface", "wlan0"),
        "hotspot_interface": hotspot_interface if hotspot_interface is not None else current.get("hotspot_interface", "wlan0"),
        "desired_mode": normalize_network_mode(
            desired_mode if desired_mode is not None else current.get("desired_mode")
        ),
    }
    _atomic_write_config(config)
    return config


def save_network_mode(desired_mode: str) -> Dict[str, Any]:
    candidate = desired_mode.strip().lower()
    if candidate not in VALID_NETWORK_MODES:
        raise ValueError(f"Unsupported network mode: {desired_mode}")

    current = load_wifi_config()
    return save_wifi_config(
        current.get("ssid"),
        bool(current.get("auto_connect", False)),
        current.get("band"),
        client_interface=current.get("client_interface"),
        hotspot_interface=current.get("hotspot_interface"),
        desired_mode=candidate,
    )
