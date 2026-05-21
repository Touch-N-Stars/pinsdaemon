import json
import os
from typing import Dict, Any

DEFAULT_HOTSPOT_PASSWORD = "touchnstars"
ALLOWED_HOTSPOT_BANDS = {"2.4GHz", "5GHz"}
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "hotspot_config.json")


def _is_valid_password(password: Any) -> bool:
    return isinstance(password, str) and 8 <= len(password) <= 63


def _normalize_band(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None

    candidate = value.strip()
    if not candidate:
        return None

    aliases = {
        "bg": "2.4GHz",
        "2.4ghz": "2.4GHz",
        "a": "5GHz",
        "5ghz": "5GHz",
    }
    normalized = aliases.get(candidate.lower(), candidate)
    if normalized in ALLOWED_HOTSPOT_BANDS:
        return normalized
    return None


def _normalize_channel(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None

    try:
        channel = int(value)
    except (TypeError, ValueError):
        return None

    if channel <= 0:
        return None
    return channel


def _validate_band_channel_pair(band: str | None, channel: int | None) -> tuple[str | None, int | None]:
    normalized_band = _normalize_band(band)
    normalized_channel = _normalize_channel(channel)
    return normalized_band, normalized_channel


def load_hotspot_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_FILE):
        return {
            "password": DEFAULT_HOTSPOT_PASSWORD,
            "band": None,
            "channel": None,
            "source": "default",
        }

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {
            "password": DEFAULT_HOTSPOT_PASSWORD,
            "band": None,
            "channel": None,
            "source": "default",
        }

    password = data.get("password")
    if not _is_valid_password(password):
        return {
            "password": DEFAULT_HOTSPOT_PASSWORD,
            "band": None,
            "channel": None,
            "source": "default",
        }

    band = _normalize_band(data.get("band"))
    channel = _normalize_channel(data.get("channel"))
    try:
        band, channel = _validate_band_channel_pair(band, channel)
    except ValueError:
        band, channel = None, None

    return {"password": password, "band": band, "channel": channel, "source": "configured"}


def save_hotspot_password(password: str) -> Dict[str, Any]:
    return save_hotspot_settings(password=password)


def save_hotspot_settings(
    password: str,
    band: str | None = None,
    channel: int | None = None,
) -> Dict[str, Any]:
    if not _is_valid_password(password):
        raise ValueError("Hotspot password must be between 8 and 63 characters")

    normalized_band, normalized_channel = _validate_band_channel_pair(band, channel)

    data = {
        "password": password,
        "band": normalized_band,
        "channel": normalized_channel,
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

    return {
        "password": password,
        "band": normalized_band,
        "channel": normalized_channel,
        "source": "configured",
    }
