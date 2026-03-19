import json
import os
from typing import Dict, Any

DEFAULT_HOTSPOT_PASSWORD = "touchnstars"
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "hotspot_config.json")


def _is_valid_password(password: Any) -> bool:
    return isinstance(password, str) and 8 <= len(password) <= 63


def load_hotspot_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_FILE):
        return {"password": DEFAULT_HOTSPOT_PASSWORD, "source": "default"}

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"password": DEFAULT_HOTSPOT_PASSWORD, "source": "default"}

    password = data.get("password")
    if not _is_valid_password(password):
        return {"password": DEFAULT_HOTSPOT_PASSWORD, "source": "default"}

    return {"password": password, "source": "configured"}


def save_hotspot_password(password: str) -> Dict[str, Any]:
    if not _is_valid_password(password):
        raise ValueError("Hotspot password must be between 8 and 63 characters")

    data = {"password": password}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

    return {"password": password, "source": "configured"}
