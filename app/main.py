import os
import json
import csv
import asyncio
import uuid
import re
import fnmatch
import tempfile
import shutil
import zipfile
import time
import subprocess
import urllib.request
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import FastAPI, Depends, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Dict, Any

from .auth import verify_token
from .job_manager import job_manager, JobStatus
from .wifi_config import load_wifi_config, save_wifi_config
from .hotspot_config import load_hotspot_config, save_hotspot_settings

app = FastAPI(title="System Update Daemon")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
# On Windows dev environment, we might want to mock the command.
# In production content, this will be the real path.
# SCRIPT_PATH = os.getenv("UPDATE_SCRIPT_PATH", "/usr/local/bin/system-upgrade.sh")
SCRIPT_PATH = os.getenv("UPDATE_SCRIPT_PATH", "/usr/local/bin/system-upgrade.sh")
SAMBA_SCRIPT_PATH = os.getenv("SAMBA_SCRIPT_PATH", "/usr/local/bin/manage-samba.sh")

# Determine default path for wifi-scan.py
# In production, it's /usr/local/bin/wifi-scan.py
# In dev (Windows/local), it might be relative.
DEFAULT_WIFI_SCAN = "/usr/local/bin/wifi-scan.py"
if not os.path.exists(DEFAULT_WIFI_SCAN):
    DEFAULT_WIFI_SCAN = os.path.join(os.path.dirname(__file__), "../scripts/wifi-scan.py")

WIFI_SCAN_SCRIPT_PATH = os.getenv("WIFI_SCAN_SCRIPT_PATH", DEFAULT_WIFI_SCAN)
DEFAULT_WIFI_AUTOMANAGE_SCRIPT = "/usr/local/bin/wifi-automanage.py"
if not os.path.exists(DEFAULT_WIFI_AUTOMANAGE_SCRIPT):
    DEFAULT_WIFI_AUTOMANAGE_SCRIPT = os.path.join(os.path.dirname(__file__), "../scripts/wifi-automanage.py")

WIFI_AUTOMANAGE_SCRIPT_PATH = os.getenv("WIFI_AUTOMANAGE_SCRIPT_PATH", DEFAULT_WIFI_AUTOMANAGE_SCRIPT)
WIFI_CONNECT_SCRIPT_PATH = os.getenv("WIFI_CONNECT_SCRIPT_PATH", "/usr/local/bin/wifi-connect.sh")
FIRMWARE_INSTALL_SCRIPT_PATH = os.getenv("FIRMWARE_INSTALL_SCRIPT_PATH", "/usr/local/bin/install-firmware.sh")
INDI_INSTALL_SCRIPT_PATH = os.getenv("INDI_INSTALL_SCRIPT_PATH", "/usr/local/bin/install-indi-package.sh")
DEFAULT_ASTAP_STAR_DATABASE_INSTALL_SCRIPT = "/usr/local/bin/install-astap-star-database.sh"
if not os.path.exists(DEFAULT_ASTAP_STAR_DATABASE_INSTALL_SCRIPT):
    DEFAULT_ASTAP_STAR_DATABASE_INSTALL_SCRIPT = os.path.join(
        os.path.dirname(__file__), "../scripts/install-astap-star-database.sh"
    )
ASTAP_STAR_DATABASE_INSTALL_SCRIPT_PATH = os.getenv(
    "ASTAP_STAR_DATABASE_INSTALL_SCRIPT_PATH", DEFAULT_ASTAP_STAR_DATABASE_INSTALL_SCRIPT
)
ASTAP_STAR_DATABASE_STATE_FILE = os.getenv(
    "ASTAP_STAR_DATABASE_STATE_FILE", "/opt/pinsdaemon/astap-star-databases.json"
)
DEFAULT_PLUGIN_MANAGE_SCRIPT = "/usr/local/bin/manage-plugin.sh"
if not os.path.exists(DEFAULT_PLUGIN_MANAGE_SCRIPT):
    DEFAULT_PLUGIN_MANAGE_SCRIPT = os.path.join(os.path.dirname(__file__), "../scripts/manage-plugin.sh")
PLUGIN_MANAGE_SCRIPT_PATH = os.getenv("PLUGIN_MANAGE_SCRIPT_PATH", DEFAULT_PLUGIN_MANAGE_SCRIPT)
DEFAULT_REQUIRED_PACKAGES_SCRIPT = "/usr/local/bin/ensure-required-packages.sh"
if not os.path.exists(DEFAULT_REQUIRED_PACKAGES_SCRIPT):
    DEFAULT_REQUIRED_PACKAGES_SCRIPT = os.path.join(os.path.dirname(__file__), "../scripts/ensure-required-packages.sh")
REQUIRED_PACKAGES_SCRIPT_PATH = os.getenv("REQUIRED_PACKAGES_SCRIPT_PATH", DEFAULT_REQUIRED_PACKAGES_SCRIPT)
STARTUP_PACKAGE_CHECK_ENABLED = os.getenv("STARTUP_PACKAGE_CHECK_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
STARTUP_WIFI_AUTOMANAGE_ENABLED = os.getenv("STARTUP_WIFI_AUTOMANAGE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
STARTUP_WIFI_AUTOMANAGE_ATTEMPTS = max(1, int(os.getenv("STARTUP_WIFI_AUTOMANAGE_ATTEMPTS", "3")))
STARTUP_WIFI_AUTOMANAGE_DELAY_SECONDS = max(0.0, float(os.getenv("STARTUP_WIFI_AUTOMANAGE_DELAY_SECONDS", "5")))
FIRMWARE_STATE_FILE = os.getenv("FIRMWARE_STATE_FILE", "/opt/pinsdaemon/firmware.txt")
FIRMWARE_UPLOAD_DIR = os.getenv("FIRMWARE_UPLOAD_DIR", "/tmp/pinsdaemon-firmware")
FIRMWARE_ZIP_RE = re.compile(r"^firmware_(\d{8})_(\d{6})\.zip$", re.IGNORECASE)
INDI_RELEASE_API_URL = os.getenv(
    "INDI_RELEASE_API_URL",
    "https://api.github.com/repos/acocalypso/indi3rdparty/releases/latest",
)
UPDATES_PACKAGES_URL = os.getenv(
    "UPDATES_PACKAGES_URL",
    "https://repo.touch-n-stars.eu/reprepro/dists/trixie/main/binary-arm64/Packages",
)
UPDATES_PACKAGE_PATTERNS = [
    p.strip() for p in os.getenv("UPDATES_PACKAGE_PATTERNS", "pins,pinsdaemon,pins-plugin-*").split(",") if p.strip()
]
AVAILABLE_PLUGIN_PACKAGES = [
    "pins-plugin-alpaca",
    "pins-plugin-groundstation",
    "pins-plugin-joko",
    "pins-plugin-livestack",
    "pins-plugin-nightsummary",
    "pins-plugin-ninaapi",
    "pins-plugin-orbitals",
    "pins-plugin-orbuculum",
    "pins-plugin-phd2tools",
    "pins-plugin-pins",
    "pins-plugin-polaralignment",
    "pins-plugin-tenmicron",
    "pins-plugin-touch-n-stars",
]
PROTECTED_PLUGIN_PACKAGES = {
    "pins-plugin-ninaapi",
    "pins-plugin-ninaapi",
    "pins-plugin-touch-n-stars",
    "pins-plugin-joko",
    "pins-plugin-polaralignment"
}
ALLOWED_INDI_3RDPARTY_TYPES = {
    "filterwheel",
    "flatpanel",
    "focuser",
    "rotator",
    "switches",
    "telescope",
    "weather",
    "camera",
}
ASTAP_STAR_DATABASES = [
    {
        "databaseId": "D50",
        "label": "D50",
        "description": "Large star database",
        "downloadUrl": "https://sourceforge.net/projects/astap-program/files/star_databases/d50_star_database.deb/download",
    },
    {
        "databaseId": "D05",
        "label": "D05",
        "description": "Smaller star database",
        "downloadUrl": "https://sourceforge.net/projects/astap-program/files/star_databases/d05_star_database.deb/download",
    },
    {
        "databaseId": "G05",
        "label": "G05",
        "description": "Wide field star database",
        "downloadUrl": "https://sourceforge.net/projects/astap-program/files/star_databases/g05_star_database.deb/download",
    },
    {
        "databaseId": "W08",
        "label": "W08",
        "description": "Very wide field star database",
        "downloadUrl": "https://sourceforge.net/projects/astap-program/files/star_databases/w08_star_database_mag08_astap.deb/download",
    },
]
ASTAP_STAR_DATABASES_BY_ID = {
    db["databaseId"]: db for db in ASTAP_STAR_DATABASES
}
UPGRADE_LAST_JOB_FILE = os.getenv("UPGRADE_LAST_JOB_FILE", "/opt/pinsdaemon/last-upgrade-job.json")
DEFAULT_DIAGNOSTICS_SCRIPT = "/usr/local/bin/collect-diagnostics.sh"
if not os.path.exists(DEFAULT_DIAGNOSTICS_SCRIPT):
    DEFAULT_DIAGNOSTICS_SCRIPT = os.path.join(os.path.dirname(__file__), "../scripts/collect-diagnostics.sh")
DIAGNOSTICS_SCRIPT_PATH = os.getenv("DIAGNOSTICS_SCRIPT_PATH", DEFAULT_DIAGNOSTICS_SCRIPT)
DIAGNOSTICS_WORK_DIR = os.getenv("DIAGNOSTICS_WORK_DIR", "/tmp/pinsdaemon-diagnostics")
DIAGNOSTICS_COLLECTION_TIMEOUT_SECONDS = max(30, int(os.getenv("DIAGNOSTICS_COLLECTION_TIMEOUT_SECONDS", "900")))
DIAGNOSTICS_RETENTION_SECONDS = max(300, int(os.getenv("DIAGNOSTICS_RETENTION_SECONDS", "86400")))
INDI_3RDPARTY_JSON_PATH = os.getenv("INDI_3RDPARTY_JSON_PATH", "/home/pi/Documents/INDI/3rdparty.json")
INDI_3RDPARTY_REGISTRY_TYPES = [
    "camera",
    "filterwheel",
    "flatpanel",
    "focuser",
    "rotator",
    "switches",
    "telescope",
    "weather",
]

class UpgradeRequest(BaseModel):
    dryRun: bool = False

class SambaRequest(BaseModel):
    enable: bool

class Phd2Request(BaseModel):
    enable: bool

class SambaStatus(BaseModel):
    enabled: bool

class Phd2Status(BaseModel):
    enabled: bool
    running: bool

class WifiNetwork(BaseModel):
    mac: Optional[str] = None
    ssid: Optional[str] = None
    signal_strength: Optional[int] = None
    quality: Optional[str] = None
    encrypted: bool = False
    channel: Optional[int] = None
    frequency: Optional[float] = None

class WifiConnectRequest(BaseModel):
    ssid: str
    password: Optional[str] = None
    auto_connect: Optional[bool] = False
    band: Optional[str] = None # "2.4GHz" or "5GHz"
    client_interface: Optional[str] = None
    hotspot_interface: Optional[str] = None

class WifiAutoConnectRequest(BaseModel):
    ssid: Optional[str] = None
    auto_connect: bool
    band: Optional[str] = None # "2.4GHz" or "5GHz" where bg=2.4 and a=5

class WifiStatusResponse(BaseModel):
    connected: bool
    ssid: Optional[str] = None
    band: Optional[str] = None # "2.4GHz" or "5GHz"


class WifiAdapterInfo(BaseModel):
    interface: str
    state: str
    connection: Optional[str] = None
    role: str
    mac: Optional[str] = None
    driver: Optional[str] = None
    mtu: Optional[int] = None


class WifiAdaptersResponse(BaseModel):
    adapters: List[WifiAdapterInfo]


class WifiInterfacesRequest(BaseModel):
    client_interface: Optional[str] = None
    hotspot_interface: Optional[str] = None


class WifiInterfacesResponse(BaseModel):
    client_interface: str
    hotspot_interface: str

class HotspotPasswordRequest(BaseModel):
    password: str
    band: Optional[str] = None
    channel: Optional[int] = None

class HotspotPasswordStatusResponse(BaseModel):
    configured: bool
    source: str
    band: Optional[str] = None
    channel: Optional[int] = None
    hotspotInterface: Optional[str] = None
    supportedChannels: Dict[str, List[int]] = {}

class HotspotPasswordUpdateResponse(BaseModel):
    status: str
    message: str
    configured: bool
    appliedToActiveHotspot: bool
    band: Optional[str] = None
    channel: Optional[int] = None


class UpdatePackageStatus(BaseModel):
    name: str
    installedVersion: Optional[str] = None
    latestVersion: Optional[str] = None
    updateAvailable: bool


class UpdatesCheckResponse(BaseModel):
    hasUpdates: bool
    checkedAt: str
    packages: List[UpdatePackageStatus]


class IndiPackageInfo(BaseModel):
    name: str
    assetName: str
    version: Optional[str] = None
    architecture: Optional[str] = None
    downloadUrl: str
    installed: bool
    installedVersion: Optional[str] = None


class IndiPackagesResponse(BaseModel):
    checkedAt: str
    onlyNotInstalled: bool
    packages: List[IndiPackageInfo]


class IndiPackageInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assetName: str
    label: Optional[str] = None
    type: Optional[str] = None


class Indi3rdpartyRegistryEntry(BaseModel):
    Name: str
    Label: str
    Type: str


class Indi3rdpartyRegistryResponse(BaseModel):
    updatedAt: str
    totalEntries: int
    entriesByType: Dict[str, List[Indi3rdpartyRegistryEntry]]


class Indi3rdpartyRegistryEntryUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    Name: Optional[str] = None
    Label: Optional[str] = None
    Type: Optional[str] = None


class AstapStarDatabaseInfo(BaseModel):
    databaseId: str
    label: str
    description: str
    downloadUrl: str
    installed: bool
    installedPackage: Optional[str] = None
    installedVersion: Optional[str] = None


class AstapStarDatabasesResponse(BaseModel):
    checkedAt: str
    onlyNotInstalled: bool
    packages: List[AstapStarDatabaseInfo]


class AstapStarDatabaseInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    databaseId: str


class PluginInfo(BaseModel):
    packageName: str
    installed: bool
    installedVersion: Optional[str] = None
    availableVersion: Optional[str] = None


class PluginsResponse(BaseModel):
    checkedAt: str
    plugins: List[PluginInfo]


class PluginActionRequest(BaseModel):
    packageName: str


_IFACE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _sanitize_interface_name(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if not _IFACE_NAME_RE.fullmatch(candidate):
        return None
    return candidate


def _validate_interface_name(value: Optional[str], field_name: str) -> Optional[str]:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if not _IFACE_NAME_RE.fullmatch(candidate):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: '{value}'")
    return candidate


def _get_configured_wifi_interfaces() -> tuple[str, str]:
    config = load_wifi_config()
    client_interface = _sanitize_interface_name(config.get("client_interface")) or "wlan0"
    hotspot_interface = _sanitize_interface_name(config.get("hotspot_interface")) or client_interface
    return client_interface, hotspot_interface


def _normalize_hotspot_band(band: Optional[str]) -> Optional[str]:
    if band is None:
        return None
    candidate = band.strip()
    if not candidate:
        return None

    aliases = {
        "2.4ghz": "2.4GHz",
        "bg": "2.4GHz",
        "5ghz": "5GHz",
        "a": "5GHz",
    }
    normalized = aliases.get(candidate.lower(), candidate)
    if normalized not in {"2.4GHz", "5GHz"}:
        raise HTTPException(status_code=400, detail="Hotspot band must be one of: 2.4GHz, 5GHz")
    return normalized


def _validate_hotspot_band_channel(
    band: Optional[str],
    channel: Optional[int],
) -> tuple[Optional[str], Optional[int]]:
    normalized_band = _normalize_hotspot_band(band)

    if channel is None:
        return normalized_band, None
    if isinstance(channel, bool):
        raise HTTPException(status_code=400, detail="Hotspot channel must be an integer")
    if channel <= 0:
        raise HTTPException(status_code=400, detail="Hotspot channel must be greater than 0")

    return normalized_band, channel


async def _read_hotspot_supported_channels(interface: str) -> Dict[str, List[int]]:
    supported: Dict[str, set[int]] = {
        "2.4GHz": set(),
        "5GHz": set(),
        "6GHz": set(),
        "60GHz": set(),
    }

    dev_proc = await asyncio.create_subprocess_exec(
        "iw",
        "dev",
        interface,
        "info",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    dev_stdout, _ = await dev_proc.communicate()
    if dev_proc.returncode != 0:
        return {"2.4GHz": [], "5GHz": [], "6GHz": [], "60GHz": []}

    wiphy_match = re.search(r"\bwiphy\s+(\d+)\b", dev_stdout.decode(errors="replace"))
    if not wiphy_match:
        return {"2.4GHz": [], "5GHz": [], "6GHz": [], "60GHz": []}

    phy_name = f"phy{wiphy_match.group(1)}"
    phy_proc = await asyncio.create_subprocess_exec(
        "iw",
        "phy",
        phy_name,
        "channels",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    phy_stdout, _ = await phy_proc.communicate()
    if phy_proc.returncode != 0:
        return {"2.4GHz": [], "5GHz": [], "6GHz": [], "60GHz": []}

    for raw_line in phy_stdout.decode(errors="replace").splitlines():
        line = raw_line.strip()
        match = re.search(r"\*\s*(\d+)\s+MHz\s+\[(\d+)\](.*)$", line)
        if not match:
            continue

        frequency_mhz = int(match.group(1))
        channel = int(match.group(2))
        flags = match.group(3).lower()
        if "disabled" in flags:
            continue

        if frequency_mhz < 3000:
            supported["2.4GHz"].add(channel)
        elif frequency_mhz < 5925:
            supported["5GHz"].add(channel)
        elif frequency_mhz < 7125:
            supported["6GHz"].add(channel)
        elif 57000 <= frequency_mhz <= 71000:
            supported["60GHz"].add(channel)

    return {
        "2.4GHz": sorted(supported["2.4GHz"]),
        "5GHz": sorted(supported["5GHz"]),
        "6GHz": sorted(supported["6GHz"]),
        "60GHz": sorted(supported["60GHz"]),
    }


def _parse_nmcli_row(line: str) -> list[str]:
    reader = csv.reader([line], delimiter=":", escapechar="\\")
    try:
        return next(reader)
    except Exception:
        return []


def _is_hotspot_connection_name(name: str) -> bool:
    return name in {"Hotspot", "hotspot-ap"} or name.startswith("pins-")


async def _read_nmcli_device_details(interface: str) -> tuple[Optional[str], Optional[str], Optional[int]]:
    proc = await asyncio.create_subprocess_exec(
        "nmcli",
        "-g",
        "GENERAL.HWADDR,GENERAL.DRIVER,GENERAL.MTU",
        "device",
        "show",
        interface,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None, None, None

    rows = [line.strip() for line in stdout.decode(errors="replace").splitlines()]
    mac = rows[0] if len(rows) > 0 and rows[0] and rows[0] != "--" else None
    driver = rows[1] if len(rows) > 1 and rows[1] and rows[1] != "--" else None

    mtu: Optional[int] = None
    if len(rows) > 2 and rows[2] and rows[2] != "--":
        try:
            mtu = int(rows[2])
        except ValueError:
            mtu = None

    return mac, driver, mtu


async def _list_wifi_adapters() -> list[WifiAdapterInfo]:
    status_proc = await asyncio.create_subprocess_exec(
        "nmcli",
        "-t",
        "-f",
        "DEVICE,TYPE,STATE,CONNECTION",
        "device",
        "status",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    status_stdout, status_stderr = await status_proc.communicate()
    if status_proc.returncode != 0:
        error_text = status_stderr.decode(errors="replace").strip() or "nmcli device status failed"
        raise HTTPException(status_code=500, detail=error_text)

    active_roles: dict[str, str] = {}
    active_proc = await asyncio.create_subprocess_exec(
        "nmcli",
        "-t",
        "-f",
        "NAME,TYPE,DEVICE",
        "connection",
        "show",
        "--active",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    active_stdout, _ = await active_proc.communicate()
    if active_proc.returncode == 0:
        for raw_line in active_stdout.decode(errors="replace").splitlines():
            fields = _parse_nmcli_row(raw_line)
            if len(fields) < 3:
                continue
            name, conn_type, device = fields[0], fields[1], fields[2]
            if conn_type != "802-11-wireless" or not device:
                continue
            active_roles[device] = "hotspot" if _is_hotspot_connection_name(name) else "client"

    adapters: list[WifiAdapterInfo] = []
    for raw_line in status_stdout.decode(errors="replace").splitlines():
        fields = _parse_nmcli_row(raw_line)
        if len(fields) < 4:
            continue

        interface, dev_type, state, connection = fields[0], fields[1], fields[2], fields[3]
        if dev_type != "wifi" or not interface:
            continue

        role = active_roles.get(interface, "idle")
        conn_name = connection if connection and connection != "--" else None
        mac, driver, mtu = await _read_nmcli_device_details(interface)

        adapters.append(
            WifiAdapterInfo(
                interface=interface,
                state=state,
                connection=conn_name,
                role=role,
                mac=mac,
                driver=driver,
                mtu=mtu,
            )
        )

    adapters.sort(key=lambda adapter: adapter.interface)
    return adapters


async def is_hotspot_active_on_interface(interface: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return False

        for line in stdout.decode(errors="replace").splitlines():
            parts = _parse_nmcli_row(line)
            if len(parts) < 3:
                continue

            name, conn_type, device = parts[0], parts[1], parts[2]
            if conn_type != "802-11-wireless" or device != interface:
                continue

            if _is_hotspot_connection_name(name):
                return True
    except Exception:
        return False

    return False

_TIMEZONE_NAME_RE = re.compile(r"^[A-Za-z0-9._+\-/]+$")


class SystemTimeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dateTime: str
    timezone: str


def _validate_timezone_name(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="timezone is required")
    if not _TIMEZONE_NAME_RE.fullmatch(candidate):
        raise HTTPException(status_code=400, detail=f"Invalid timezone: {value}")
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        raise HTTPException(status_code=400, detail=f"Unknown timezone: {value}")
    return candidate


def _parse_request_datetime(value: str) -> datetime:
    candidate = value.strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="dateTime is required")

    # Accept common UTC suffix and parse ISO datetime.
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"

    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        raise HTTPException(status_code=400, detail="dateTime must be a valid ISO-8601 datetime")

class PiTemperatureResponse(BaseModel):
    celsius: float
    fahrenheit: float
    source: str

class JobResponse(BaseModel):

    jobId: str
    status: JobStatus
    exitCode: Optional[int]
    startedAt: float
    finishedAt: Optional[float]
    command: str

class FirmwareUploadResponse(BaseModel):
    status: str
    message: str
    firmwareTag: str
    currentFirmwareTag: Optional[str] = None
    job: Optional[JobResponse] = None


def parse_firmware_zip_name(filename: str) -> tuple[str, datetime]:
    """Parse firmware_DDMMYYYY_HHMMSS.zip into a comparable datetime."""
    base_name = os.path.basename(filename)
    match = FIRMWARE_ZIP_RE.match(base_name)
    if not match:
        raise ValueError("Filename must match firmware_DDMMYYYY_HHMMSS.zip")

    date_part, time_part = match.group(1), match.group(2)
    dt = datetime.strptime(f"{date_part}{time_part}", "%d%m%Y%H%M%S")
    tag = f"firmware_{date_part}_{time_part}"
    return tag, dt


def read_installed_firmware() -> tuple[Optional[str], Optional[datetime]]:
    if not os.path.exists(FIRMWARE_STATE_FILE):
        return None, None

    try:
        with open(FIRMWARE_STATE_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except Exception:
        return None, None

    match = re.search(r"firmware_(\d{8})_(\d{6})", content, flags=re.IGNORECASE)
    if not match:
        return None, None

    date_part, time_part = match.group(1), match.group(2)
    tag = f"firmware_{date_part}_{time_part}"
    try:
        dt = datetime.strptime(f"{date_part}{time_part}", "%d%m%Y%H%M%S")
    except ValueError:
        return tag, None
    return tag, dt


def _fetch_packages_index(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "pinsdaemon-update-check/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _fetch_indi_release_assets(api_url: str) -> list[dict[str, str]]:
    req = urllib.request.Request(api_url, headers={"User-Agent": "pinsdaemon-indi-packages/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))

    assets = payload.get("assets", [])
    result: list[dict[str, str]] = []
    for asset in assets:
        name = asset.get("name")
        download_url = asset.get("browser_download_url")
        if isinstance(name, str) and isinstance(download_url, str):
            result.append({"name": name, "downloadUrl": download_url})
    return result


def _parse_packages_versions(packages_text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    current: dict[str, str] = {}

    def flush_entry(entry: dict[str, str]):
        name = entry.get("Package")
        version = entry.get("Version")
        if name and version:
            parsed[name] = version

    for raw_line in packages_text.splitlines():
        line = raw_line.rstrip("\n")
        if not line.strip():
            if current:
                flush_entry(current)
                current = {}
            continue
        if line.startswith((" ", "\t")):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current[key.strip()] = value.strip()

    if current:
        flush_entry(current)

    return parsed


def _is_dbg_build_asset(asset_name: str) -> bool:
    normalized = asset_name.lower()
    return (
        "dbgsym" in normalized
        or "-dbg_" in normalized
        or "_dbg_" in normalized
        or normalized.endswith("-dbg.deb")
        or normalized.endswith("_dbg.deb")
    )


def _parse_deb_asset(asset_name: str) -> tuple[str, Optional[str], Optional[str]]:
    if not asset_name.endswith(".deb"):
        raise ValueError("Not a .deb package")

    m = re.match(r"^(?P<name>.+?)_(?P<version>[^_]+)_(?P<arch>[^_]+)\.deb$", asset_name)
    if m:
        return m.group("name"), m.group("version"), m.group("arch")

    # Fallback for uncommon names without standard Debian filename layout.
    return asset_name[:-4], None, None


async def _debian_version_gt(candidate: str, baseline: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "dpkg",
        "--compare-versions",
        candidate,
        "gt",
        baseline,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return (await proc.wait()) == 0


async def _get_installed_package_versions() -> dict[str, str]:
    proc = await asyncio.create_subprocess_exec(
        "dpkg-query",
        "-W",
        "-f=${Package}\t${Version}\n",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return {}

    versions: dict[str, str] = {}
    for line in stdout.decode(errors="replace").splitlines():
        if "\t" not in line:
            continue
        name, version = line.split("\t", 1)
        name = name.strip()
        version = version.strip()
        if not name or not version:
            continue
        versions[name] = version
    return versions


async def _build_indi_packages(only_not_installed: bool, name_filter: Optional[str]) -> list[IndiPackageInfo]:
    assets = await asyncio.to_thread(_fetch_indi_release_assets, INDI_RELEASE_API_URL)
    installed_versions = await _get_installed_package_versions()

    query = (name_filter or "").strip().lower()
    result: list[IndiPackageInfo] = []

    for asset in assets:
        asset_name = asset["name"]
        download_url = asset["downloadUrl"]

        if not asset_name.endswith(".deb"):
            continue
        if _is_dbg_build_asset(asset_name):
            continue

        package_name, package_version, package_arch = _parse_deb_asset(asset_name)
        installed_version = installed_versions.get(package_name)
        installed = installed_version is not None

        if only_not_installed and installed:
            continue

        if query and query not in package_name.lower() and query not in asset_name.lower():
            continue

        result.append(
            IndiPackageInfo(
                name=package_name,
                assetName=asset_name,
                version=package_version,
                architecture=package_arch,
                downloadUrl=download_url,
                installed=installed,
                installedVersion=installed_version,
            )
        )

    result.sort(key=lambda p: (p.name, p.assetName))
    return result


def _matches_any_pattern(package_name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(package_name, pattern) for pattern in patterns)


def _validate_plugin_package_name(package_name: str, *, for_action: bool = False) -> str:
    candidate = package_name.strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="packageName is required")
    if candidate not in AVAILABLE_PLUGIN_PACKAGES:
        raise HTTPException(status_code=400, detail=f"Unknown plugin package: {candidate}")
    if for_action and candidate in PROTECTED_PLUGIN_PACKAGES:
        raise HTTPException(status_code=403, detail=f"Package cannot be installed or removed: {candidate}")
    return candidate


def _normalize_indi_3rdparty_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None

    candidate = value.strip().lower()
    if not candidate:
        return None

    aliases = {
        "cameras": "camera",
        "ccd": "camera",
        "ccds": "camera",
        "filterwheels": "filterwheel",
        "flatpanels": "flatpanel",
        "focusers": "focuser",
        "rotators": "rotator",
        "switch": "switches",
        "telescopes": "telescope",
    }
    normalized = aliases.get(candidate, candidate)

    if normalized not in ALLOWED_INDI_3RDPARTY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid INDI 3rdparty type: {value}. Allowed: {', '.join(sorted(ALLOWED_INDI_3RDPARTY_TYPES))}",
        )
    return normalized


def _normalize_optional_text(value: Optional[str], field_name: str) -> Optional[str]:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if len(candidate) > 200:
        raise HTTPException(status_code=400, detail=f"{field_name} is too long")
    return candidate


def _default_indi_3rdparty_registry() -> Dict[str, List[Dict[str, str]]]:
    return {bucket: [] for bucket in INDI_3RDPARTY_REGISTRY_TYPES}


def _read_indi_3rdparty_registry_text() -> str:
    try:
        with open(INDI_3RDPARTY_JSON_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except PermissionError:
        proc = subprocess.run(
            ["sudo", "-n", "cat", INDI_3RDPARTY_JSON_PATH],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            reason = proc.stderr.strip() or f"exit code {proc.returncode}"
            raise HTTPException(status_code=500, detail=f"Unable to read INDI 3rdparty registry: {reason}")
        return proc.stdout
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Unable to read INDI 3rdparty registry: {exc}")


def _load_indi_3rdparty_registry() -> Dict[str, List[Dict[str, str]]]:
    normalized = _default_indi_3rdparty_registry()
    text = _read_indi_3rdparty_registry_text()
    if not text.strip():
        return normalized

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"INDI 3rdparty registry is invalid JSON: {exc}")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="INDI 3rdparty registry root must be a JSON object")

    for bucket in INDI_3RDPARTY_REGISTRY_TYPES:
        raw_entries = payload.get(bucket, [])
        if not isinstance(raw_entries, list):
            continue

        seen_by_name: dict[str, Dict[str, str]] = {}
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue

            name = str(raw.get("Name", "")).strip()
            if not name:
                continue

            label_raw = raw.get("Label")
            label = str(label_raw).strip() if isinstance(label_raw, str) else ""
            if not label:
                label = name

            seen_by_name[name] = {
                "Name": name,
                "Label": label,
                "Type": bucket,
            }

        normalized[bucket] = sorted(seen_by_name.values(), key=lambda entry: entry["Name"].lower())

    return normalized


def _write_indi_3rdparty_registry(data: Dict[str, List[Dict[str, str]]]) -> None:
    payload = json.dumps(data, indent=2) + "\n"
    path = INDI_3RDPARTY_JSON_PATH
    directory = os.path.dirname(path)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except PermissionError:
            # Directory might be root-owned; fallback writer below may still succeed.
            pass

    temp_path = f"{path}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(temp_path, path)
        return
    except PermissionError:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass

        proc = subprocess.run(
            ["sudo", "-n", "tee", path],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            reason = proc.stderr.strip() or f"exit code {proc.returncode}"
            raise HTTPException(status_code=500, detail=f"Unable to write INDI 3rdparty registry: {reason}")
    except OSError as exc:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Unable to write INDI 3rdparty registry: {exc}")


def _build_indi_3rdparty_registry_response(data: Dict[str, List[Dict[str, str]]]) -> Indi3rdpartyRegistryResponse:
    total_entries = sum(len(entries) for entries in data.values())
    return Indi3rdpartyRegistryResponse(
        updatedAt=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        totalEntries=total_entries,
        entriesByType={
            bucket: [Indi3rdpartyRegistryEntry(**entry) for entry in data.get(bucket, [])]
            for bucket in INDI_3RDPARTY_REGISTRY_TYPES
        },
    )


def _normalize_astap_star_database_id(value: str) -> str:
    candidate = value.strip().upper()
    if not candidate:
        raise HTTPException(status_code=400, detail="databaseId is required")
    if candidate not in ASTAP_STAR_DATABASES_BY_ID:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported ASTAP star database: {value}. Allowed: {', '.join(ASTAP_STAR_DATABASES_BY_ID.keys())}",
        )
    return candidate


def _read_astap_star_database_state() -> dict[str, dict[str, str]]:
    if not os.path.exists(ASTAP_STAR_DATABASE_STATE_FILE):
        return {}

    try:
        with open(ASTAP_STAR_DATABASE_STATE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {}

    if not isinstance(payload, dict):
        return {}

    raw_databases = payload.get("databases")
    if not isinstance(raw_databases, dict):
        return {}

    normalized: dict[str, dict[str, str]] = {}
    for database_id, raw_entry in raw_databases.items():
        if not isinstance(database_id, str) or not isinstance(raw_entry, dict):
            continue

        package_name = raw_entry.get("packageName")
        if not isinstance(package_name, str):
            continue

        package_name = package_name.strip()
        if not package_name:
            continue

        normalized[database_id.strip().upper()] = {"packageName": package_name}

    return normalized


def _find_astap_package_candidates(database_id: str, installed_versions: dict[str, str]) -> list[str]:
    marker = database_id.lower()
    candidates: list[str] = []

    for package_name in installed_versions.keys():
        normalized = package_name.lower()
        if marker not in normalized:
            continue
        if "database" not in normalized and "star" not in normalized and "astap" not in normalized:
            continue
        candidates.append(package_name)

    return sorted(candidates)


async def _build_astap_star_databases(
    only_not_installed: bool,
    name_filter: Optional[str],
) -> list[AstapStarDatabaseInfo]:
    installed_versions = await _get_installed_package_versions()
    state_by_database = _read_astap_star_database_state()

    query = (name_filter or "").strip().lower()
    result: list[AstapStarDatabaseInfo] = []

    for database in ASTAP_STAR_DATABASES:
        database_id = database["databaseId"]
        label = database["label"]
        description = database["description"]

        if query:
            searchable = f"{database_id} {label} {description}".lower()
            if query not in searchable:
                continue

        installed = False
        installed_package: Optional[str] = None
        installed_version: Optional[str] = None

        state_entry = state_by_database.get(database_id)
        if state_entry:
            state_package = state_entry.get("packageName")
            if state_package:
                installed_package = state_package
                installed_version = installed_versions.get(state_package)
                installed = installed_version is not None

        if not installed:
            for candidate in _find_astap_package_candidates(database_id, installed_versions):
                installed_package = candidate
                installed_version = installed_versions.get(candidate)
                installed = installed_version is not None
                if installed:
                    break

        if only_not_installed and installed:
            continue

        result.append(
            AstapStarDatabaseInfo(
                databaseId=database_id,
                label=label,
                description=description,
                downloadUrl=database["downloadUrl"],
                installed=installed,
                installedPackage=installed_package,
                installedVersion=installed_version,
            )
        )

    return result


def _write_last_upgrade_job_state(payload: Dict[str, Any]) -> None:
    try:
        state_dir = os.path.dirname(UPGRADE_LAST_JOB_FILE)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)

        temp_path = f"{UPGRADE_LAST_JOB_FILE}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(temp_path, UPGRADE_LAST_JOB_FILE)
    except Exception as e:
        print(f"Warning: failed to persist latest upgrade job state: {e}")


def _read_last_upgrade_job_state() -> Optional[Dict[str, Any]]:
    if not os.path.exists(UPGRADE_LAST_JOB_FILE):
        return None
    try:
        with open(UPGRADE_LAST_JOB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def _job_response_from_state(data: Optional[Dict[str, Any]]) -> Optional[JobResponse]:
    if not data:
        return None
    try:
        status = JobStatus(data["status"])
        exit_code = data.get("exitCode")
        started_at = float(data["startedAt"])
        finished_at_raw = data.get("finishedAt")
        finished_at = float(finished_at_raw) if finished_at_raw is not None else None
        return JobResponse(
            jobId=str(data["jobId"]),
            status=status,
            exitCode=int(exit_code) if isinstance(exit_code, (int, str)) and str(exit_code).strip() != "" else None,
            startedAt=started_at,
            finishedAt=finished_at,
            command=str(data.get("command", "")),
        )
    except Exception:
        return None


def _job_response_from_runtime_job(job) -> JobResponse:
    return JobResponse(
        jobId=job.id,
        status=job.status,
        exitCode=job.exit_code,
        startedAt=job.created_at,
        finishedAt=job.finished_at,
        command=job.command,
    )


async def _ensure_required_packages_on_startup() -> None:
    if not STARTUP_PACKAGE_CHECK_ENABLED:
        print("Startup required package check is disabled.")
        return

    if not os.path.exists(REQUIRED_PACKAGES_SCRIPT_PATH):
        print(f"Required packages script not found at {REQUIRED_PACKAGES_SCRIPT_PATH}; skipping startup package check.")
        return

    print(f"Starting required package check using {REQUIRED_PACKAGES_SCRIPT_PATH}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", REQUIRED_PACKAGES_SCRIPT_PATH,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            decoded_line = line.decode(errors="replace").rstrip()
            if decoded_line:
                print(f"[startup-package-check] {decoded_line}")

        return_code = await proc.wait()
        if return_code == 0:
            print("Startup required package check finished successfully.")
        else:
            print(f"Startup required package check failed with exit code {return_code}.")
    except Exception as e:
        print(f"Startup required package check failed to execute: {e}")


async def _run_wifi_automanage(reason: str, attempts: int, delay_seconds: float) -> None:
    if not os.path.exists(WIFI_AUTOMANAGE_SCRIPT_PATH):
        print(f"Wi-Fi auto-manage script not found at {WIFI_AUTOMANAGE_SCRIPT_PATH}; skipping startup Wi-Fi auto-manage.")
        return

    attempts = max(1, attempts)
    delay_seconds = max(0.0, delay_seconds)

    print(f"Starting Wi-Fi auto-manage ({reason}) using {WIFI_AUTOMANAGE_SCRIPT_PATH}")
    for attempt in range(1, attempts + 1):
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "-n", WIFI_AUTOMANAGE_SCRIPT_PATH,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                decoded_line = line.decode(errors="replace").rstrip()
                if decoded_line:
                    print(f"[wifi-automanage:{reason}] {decoded_line}")

            return_code = await proc.wait()
            if return_code == 0:
                print(f"Wi-Fi auto-manage ({reason}) finished successfully.")
                return

            print(
                f"Wi-Fi auto-manage ({reason}) attempt {attempt}/{attempts} "
                f"failed with exit code {return_code}."
            )
        except Exception as e:
            print(f"Wi-Fi auto-manage ({reason}) attempt {attempt}/{attempts} failed to execute: {e}")

        if attempt < attempts and delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

    print(f"Wi-Fi auto-manage ({reason}) failed after all retry attempts.")


async def _run_wifi_automanage_on_startup() -> None:
    if not STARTUP_WIFI_AUTOMANAGE_ENABLED:
        print("Startup Wi-Fi auto-manage is disabled.")
        return

    await _run_wifi_automanage(
        reason="startup",
        attempts=STARTUP_WIFI_AUTOMANAGE_ATTEMPTS,
        delay_seconds=STARTUP_WIFI_AUTOMANAGE_DELAY_SECONDS,
    )


@app.on_event("startup")
async def schedule_startup_tasks() -> None:
    asyncio.create_task(_ensure_required_packages_on_startup())
    asyncio.create_task(_run_wifi_automanage_on_startup())

@app.post("/upgrade", response_model=JobResponse, dependencies=[Depends(verify_token)])
async def trigger_upgrade(request: UpgradeRequest):
    """
    Triggers the system upgrade script.
    """
    # Create specific job ID to track the systemd unit
    job_id = str(uuid.uuid4())
    
    # Construct command
    # Using 'sudo' + script path.
    # Note: verify sudoers is set up correctly.
    cmd = ["sudo", "-n", SCRIPT_PATH, "--job-id", job_id, "--state-file", UPGRADE_LAST_JOB_FILE]
    if request.dryRun:
        # Pass a flag if the script supports it, or just log meant for dry run.
        # Assuming the script takes --dry-run
        cmd.append("--dry-run")

    # Pass the pre-generated ID so job manager uses it
    await job_manager.start_job(cmd, job_id=job_id)
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=500, detail="Failed to create upgrade job")

    response = _job_response_from_runtime_job(job)
    _write_last_upgrade_job_state(response.model_dump())
    return response


@app.get("/updates/check", response_model=UpdatesCheckResponse, dependencies=[Depends(verify_token)])
async def check_updates():
    try:
        packages_text = await asyncio.to_thread(_fetch_packages_index, UPDATES_PACKAGES_URL)
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch repo metadata: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch repo metadata: {e}")

    repo_versions_all = _parse_packages_versions(packages_text)
    repo_versions = {
        name: version
        for name, version in repo_versions_all.items()
        if _matches_any_pattern(name, UPDATES_PACKAGE_PATTERNS)
    }
    installed_versions_all = await _get_installed_package_versions()
    installed_versions = {
        name: version
        for name, version in installed_versions_all.items()
        if _matches_any_pattern(name, UPDATES_PACKAGE_PATTERNS)
    }

    package_names = sorted(set(repo_versions.keys()) | set(installed_versions.keys()))

    result_packages: list[UpdatePackageStatus] = []
    has_updates = False
    for name in package_names:
        installed_version = installed_versions.get(name)
        latest_version = repo_versions.get(name)
        update_available = False
        if installed_version and latest_version:
            update_available = await _debian_version_gt(latest_version, installed_version)

        if update_available:
            has_updates = True

        result_packages.append(
            UpdatePackageStatus(
                name=name,
                installedVersion=installed_version,
                latestVersion=latest_version,
                updateAvailable=update_available,
            )
        )

    checked_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    return UpdatesCheckResponse(hasUpdates=has_updates, checkedAt=checked_at, packages=result_packages)


@app.get("/packages/indi3rdparty", response_model=IndiPackagesResponse, dependencies=[Depends(verify_token)])
async def list_indi3rdparty_packages(onlyNotInstalled: bool = False, q: Optional[str] = None):
    try:
        packages = await _build_indi_packages(only_not_installed=onlyNotInstalled, name_filter=q)
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch release metadata: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to build package list: {e}")

    checked_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    return IndiPackagesResponse(checkedAt=checked_at, onlyNotInstalled=onlyNotInstalled, packages=packages)


@app.get("/packages/indi3rdparty/registry", response_model=Indi3rdpartyRegistryResponse, dependencies=[Depends(verify_token)])
async def get_indi3rdparty_registry():
    data = _load_indi_3rdparty_registry()
    return _build_indi_3rdparty_registry_response(data)


@app.patch("/packages/indi3rdparty/registry/{entry_name}", response_model=Indi3rdpartyRegistryResponse, dependencies=[Depends(verify_token)])
async def update_indi3rdparty_registry_entry(entry_name: str, request: Indi3rdpartyRegistryEntryUpdateRequest):
    current_name = entry_name.strip()
    if not current_name:
        raise HTTPException(status_code=400, detail="entry_name is required")

    data = _load_indi_3rdparty_registry()

    source_bucket: Optional[str] = None
    source_index: Optional[int] = None
    source_entry: Optional[Dict[str, str]] = None

    for bucket in INDI_3RDPARTY_REGISTRY_TYPES:
        entries = data.get(bucket, [])
        for idx, entry in enumerate(entries):
            if entry.get("Name") == current_name:
                source_bucket = bucket
                source_index = idx
                source_entry = entry
                break
        if source_entry:
            break

    if not source_entry or source_bucket is None or source_index is None:
        raise HTTPException(status_code=404, detail=f"INDI 3rdparty entry not found: {current_name}")

    target_name = _normalize_optional_text(request.Name, "Name") or source_entry["Name"]
    target_label = _normalize_optional_text(request.Label, "Label") or source_entry["Label"]
    target_type = _normalize_indi_3rdparty_type(request.Type) or source_entry["Type"]

    if target_name != source_entry["Name"]:
        for bucket in INDI_3RDPARTY_REGISTRY_TYPES:
            for existing in data.get(bucket, []):
                if existing.get("Name") == target_name:
                    raise HTTPException(status_code=409, detail=f"INDI 3rdparty entry already exists: {target_name}")

    # Remove original entry from its current bucket.
    data[source_bucket].pop(source_index)

    updated_entry = {
        "Name": target_name,
        "Label": target_label,
        "Type": target_type,
    }

    data.setdefault(target_type, []).append(updated_entry)

    # Keep output deterministic and avoid duplicates by Name per bucket.
    for bucket in INDI_3RDPARTY_REGISTRY_TYPES:
        deduped: dict[str, Dict[str, str]] = {}
        for entry in data.get(bucket, []):
            name = entry.get("Name", "")
            if not name:
                continue
            deduped[name] = {
                "Name": name,
                "Label": entry.get("Label", name),
                "Type": bucket,
            }
        data[bucket] = sorted(deduped.values(), key=lambda entry: entry["Name"].lower())

    _write_indi_3rdparty_registry(data)
    return _build_indi_3rdparty_registry_response(data)


@app.get("/packages/astap/stardatabases", response_model=AstapStarDatabasesResponse, dependencies=[Depends(verify_token)])
async def list_astap_star_databases(onlyNotInstalled: bool = True, q: Optional[str] = None):
    try:
        packages = await _build_astap_star_databases(only_not_installed=onlyNotInstalled, name_filter=q)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to build ASTAP package list: {e}")

    checked_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    return AstapStarDatabasesResponse(checkedAt=checked_at, onlyNotInstalled=onlyNotInstalled, packages=packages)


@app.post("/packages/astap/stardatabases/install", response_model=JobResponse, dependencies=[Depends(verify_token)])
async def install_astap_star_database(request: AstapStarDatabaseInstallRequest):
    database_id = _normalize_astap_star_database_id(request.databaseId)

    if not os.path.exists(ASTAP_STAR_DATABASE_INSTALL_SCRIPT_PATH):
        raise HTTPException(
            status_code=500,
            detail=f"ASTAP installer script not found at {ASTAP_STAR_DATABASE_INSTALL_SCRIPT_PATH}",
        )

    available = await _build_astap_star_databases(only_not_installed=False, name_filter=None)
    selected = next((package for package in available if package.databaseId == database_id), None)
    if selected and selected.installed:
        raise HTTPException(status_code=409, detail=f"ASTAP star database {database_id} is already installed")

    cmd = ["sudo", "-n", ASTAP_STAR_DATABASE_INSTALL_SCRIPT_PATH, database_id]
    job_id = await job_manager.start_job(cmd)
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=500, detail="Failed to create ASTAP install job")

    return JobResponse(
        jobId=job.id,
        status=job.status,
        exitCode=job.exit_code,
        startedAt=job.created_at,
        finishedAt=job.finished_at,
        command=job.command,
    )


@app.get("/plugins", response_model=PluginsResponse, dependencies=[Depends(verify_token)])
async def list_plugins():
    try:
        packages_text = await asyncio.to_thread(_fetch_packages_index, UPDATES_PACKAGES_URL)
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch repo metadata: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch repo metadata: {e}")

    repo_versions = _parse_packages_versions(packages_text)
    installed_versions = await _get_installed_package_versions()

    plugins = [
        PluginInfo(
            packageName=package_name,
            installed=package_name in installed_versions,
            installedVersion=installed_versions.get(package_name),
            availableVersion=repo_versions.get(package_name),
        )
        for package_name in AVAILABLE_PLUGIN_PACKAGES
    ]

    checked_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    return PluginsResponse(checkedAt=checked_at, plugins=plugins)


@app.post("/plugins/install", response_model=JobResponse, dependencies=[Depends(verify_token)])
async def install_plugin(request: PluginActionRequest):
    package_name = _validate_plugin_package_name(request.packageName, for_action=True)

    if not os.path.exists(PLUGIN_MANAGE_SCRIPT_PATH):
        raise HTTPException(status_code=500, detail=f"Plugin management script not found at {PLUGIN_MANAGE_SCRIPT_PATH}")

    cmd = ["sudo", "-n", PLUGIN_MANAGE_SCRIPT_PATH, "install", package_name]

    job_id = await job_manager.start_job(cmd)
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=500, detail="Failed to create plugin install job")

    return JobResponse(
        jobId=job.id,
        status=job.status,
        exitCode=job.exit_code,
        startedAt=job.created_at,
        finishedAt=job.finished_at,
        command=job.command,
    )


@app.post("/plugins/uninstall", response_model=JobResponse, dependencies=[Depends(verify_token)])
async def uninstall_plugin(request: PluginActionRequest):
    package_name = _validate_plugin_package_name(request.packageName, for_action=True)

    if not os.path.exists(PLUGIN_MANAGE_SCRIPT_PATH):
        raise HTTPException(status_code=500, detail=f"Plugin management script not found at {PLUGIN_MANAGE_SCRIPT_PATH}")

    cmd = ["sudo", "-n", PLUGIN_MANAGE_SCRIPT_PATH, "uninstall", package_name]

    job_id = await job_manager.start_job(cmd)
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=500, detail="Failed to create plugin uninstall job")

    return JobResponse(
        jobId=job.id,
        status=job.status,
        exitCode=job.exit_code,
        startedAt=job.created_at,
        finishedAt=job.finished_at,
        command=job.command,
    )


@app.post("/packages/indi3rdparty/install", response_model=JobResponse, dependencies=[Depends(verify_token)])
async def install_indi3rdparty_package(request: IndiPackageInstallRequest):
    target_asset = request.assetName.strip()
    if not target_asset:
        raise HTTPException(status_code=400, detail="assetName is required")

    entry_type = _normalize_indi_3rdparty_type(request.type)
    entry_label = _normalize_optional_text(request.label, "label")

    try:
        packages = await _build_indi_packages(only_not_installed=False, name_filter=None)
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch release metadata: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to build package list: {e}")

    selected = next((p for p in packages if p.assetName == target_asset), None)
    if not selected:
        raise HTTPException(status_code=404, detail="Selected package asset not found in latest release")

    cmd = ["sudo", "-n", INDI_INSTALL_SCRIPT_PATH, selected.downloadUrl, selected.assetName]
    if entry_type:
        cmd.extend(["--type", entry_type])
    if entry_label:
        cmd.extend(["--label", entry_label])
    job_id = await job_manager.start_job(cmd)
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=500, detail="Failed to create install job")

    return JobResponse(
        jobId=job.id,
        status=job.status,
        exitCode=job.exit_code,
        startedAt=job.created_at,
        finishedAt=job.finished_at,
        command=job.command,
    )


@app.post("/firmware/upload", response_model=FirmwareUploadResponse, dependencies=[Depends(verify_token)])
async def upload_firmware(file: UploadFile = File(...)):
    """
    Uploads a firmware zip, compares version date against installed firmware,
    and starts async installation of contained .deb packages if newer.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="Uploaded file must include a filename")

    try:
        uploaded_tag, uploaded_dt = parse_firmware_zip_name(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    current_tag, current_dt = read_installed_firmware()
    if current_dt and uploaded_dt <= current_dt:
        await file.close()
        return FirmwareUploadResponse(
            status="up_to_date",
            message="Firmware is already up to date",
            firmwareTag=uploaded_tag,
            currentFirmwareTag=current_tag,
            job=None,
        )

    os.makedirs(FIRMWARE_UPLOAD_DIR, exist_ok=True)
    target_name = f"{uuid.uuid4()}_{os.path.basename(file.filename)}"
    uploaded_path = os.path.join(FIRMWARE_UPLOAD_DIR, target_name)

    try:
        with open(uploaded_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded firmware: {e}")
    finally:
        await file.close()

    cmd = [
        "sudo",
        "-n",
        FIRMWARE_INSTALL_SCRIPT_PATH,
        uploaded_path,
        uploaded_tag,
        FIRMWARE_STATE_FILE,
    ]

    job_id = await job_manager.start_job(cmd)
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=500, detail="Failed to create firmware installation job")

    return FirmwareUploadResponse(
        status="started",
        message="Firmware upload complete. Installation started.",
        firmwareTag=uploaded_tag,
        currentFirmwareTag=current_tag,
        job=JobResponse(
            jobId=job.id,
            status=job.status,
            exitCode=job.exit_code,
            startedAt=job.created_at,
            finishedAt=job.finished_at,
            command=job.command,
        ),
    )


@app.get("/samba", response_model=SambaStatus, dependencies=[Depends(verify_token)])
async def get_samba_status():
    """
    Check if Samba is enabled.
    """
    try:
        # Run the manage-samba.sh script with 'status' argument
        cmd = ["sudo", "-n", SAMBA_SCRIPT_PATH, "status"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode().strip()
        
        return SambaStatus(enabled=(output == "enabled"))
    except Exception as e:
        # Log error? Return false?
        print(f"Error checking samba status: {e}")
        return SambaStatus(enabled=False)


@app.post("/samba", response_model=JobResponse, dependencies=[Depends(verify_token)])
async def trigger_samba(request: SambaRequest):
    """
    Enable or Disable Samba (SMB) Share.
    """
    action = "enable" if request.enable else "disable"
    cmd = ["sudo", "-n", SAMBA_SCRIPT_PATH, action]
    
    job_id = await job_manager.start_job(cmd)
    job = job_manager.get_job(job_id)
    
    return JobResponse(
        jobId=job.id,
        status=job.status,
        exitCode=job.exit_code,
        startedAt=job.created_at,
        finishedAt=job.finished_at,
        command=job.command
    )


@app.get("/phd2", response_model=Phd2Status, dependencies=[Depends(verify_token)])
async def get_phd2_status():
    """
    Check if PHD2 service is running and enabled.
    """
    async def check_cmd(args):
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.wait()
            return proc.returncode == 0
        except:
            return False

    is_active = await check_cmd(["systemctl", "is-active", "phd2"])
    is_enabled = await check_cmd(["systemctl", "is-enabled", "phd2"])
    
    return Phd2Status(enabled=is_enabled, running=is_active)


@app.post("/phd2", response_model=JobResponse, dependencies=[Depends(verify_token)])
async def manage_phd2(request: Phd2Request):
    """
    Enable/Start or Disable/Stop PHD2 service.
    """
    # Use --now to start/enable or stop/disable immediately
    action = "enable" if request.enable else "disable"
    # Ensure checking/enabling logic is correct:
    # enable --now: enables and starts (if not running)
    # disable --now: disables and stops (if running)
    cmd = ["sudo", "-n", "systemctl", action, "--now", "phd2"]
    
    job_id = await job_manager.start_job(cmd)
    job = job_manager.get_job(job_id)
    
    return JobResponse(
        jobId=job.id,
        status=job.status,
        exitCode=job.exit_code,
        startedAt=job.created_at,
        finishedAt=job.finished_at,
        command=job.command
    )


@app.get("/jobs/latest", response_model=JobResponse, dependencies=[Depends(verify_token)])
async def get_latest_job_status():
    runtime_job = job_manager.get_latest_job()
    runtime_response = _job_response_from_runtime_job(runtime_job) if runtime_job else None

    stored_response = _job_response_from_state(_read_last_upgrade_job_state())

    if runtime_response and stored_response:
        if stored_response.startedAt >= runtime_response.startedAt:
            return stored_response
        return runtime_response

    if runtime_response:
        return runtime_response
    if stored_response:
        return stored_response

    raise HTTPException(status_code=404, detail="No jobs found")


@app.get("/jobs/{job_id}", response_model=JobResponse, dependencies=[Depends(verify_token)])
async def get_job_status(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        stored_response = _job_response_from_state(_read_last_upgrade_job_state())
        if stored_response and stored_response.jobId == job_id:
            return stored_response
        raise HTTPException(status_code=404, detail="Job not found")

    return _job_response_from_runtime_job(job)

@app.websocket("/logs/{job_id}")
async def websocket_logs(websocket: WebSocket, job_id: str):
    # Note: WebSocket cannot use the standard HTTPBearer dependency easily 
    # because headers are handled differently in WS handshake or not supported by some clients in standard ways.
    # Often tokens are passed in query param for WS: ?token=...
    # For simplicity here, we'll check query param.
    
    token = websocket.query_params.get("token")
    if token != os.getenv("API_TOKEN", "change-me-please"):
        await websocket.close(code=1008) # Policy Violation
        return

    job = job_manager.get_job(job_id)
    if not job:
        await websocket.close(code=1000, reason="Job not found")
        return

    await websocket.accept()

    # 1. Send past logs
    for line in job.logs:
        await websocket.send_text(line)

    # 2. If job is finished, close
    if job.finished_at is not None:
        await websocket.close()
        return

    # 3. Listen for live logs
    listener_queue = job.register_listener()
    try:
        while True:
            line = await listener_queue.get()
            if line is None:
                # End of stream signal
                break
            await websocket.send_text(line)
    except WebSocketDisconnect:
        # Client disconnected
        pass
    finally:
        job.remove_listener(listener_queue)
        # Only close if not already closed
        try:
            await websocket.close()
        except:
            pass


@app.get("/wifi/adapters", response_model=WifiAdaptersResponse, dependencies=[Depends(verify_token)])
async def list_wifi_adapters():
    adapters = await _list_wifi_adapters()
    return WifiAdaptersResponse(adapters=adapters)


@app.get("/wifi/interfaces", response_model=WifiInterfacesResponse, dependencies=[Depends(verify_token)])
async def get_wifi_interfaces():
    client_interface, hotspot_interface = _get_configured_wifi_interfaces()
    return WifiInterfacesResponse(client_interface=client_interface, hotspot_interface=hotspot_interface)


@app.post("/wifi/interfaces", response_model=WifiInterfacesResponse, dependencies=[Depends(verify_token)])
async def set_wifi_interfaces(request: WifiInterfacesRequest):
    client_interface_input = _validate_interface_name(request.client_interface, "client_interface")
    hotspot_interface_input = _validate_interface_name(request.hotspot_interface, "hotspot_interface")

    current = load_wifi_config()
    current_client = _sanitize_interface_name(current.get("client_interface")) or "wlan0"
    current_hotspot = _sanitize_interface_name(current.get("hotspot_interface")) or current_client

    client_interface = client_interface_input if client_interface_input is not None else current_client
    hotspot_interface = hotspot_interface_input if hotspot_interface_input is not None else current_hotspot

    requested_interfaces = set()
    if client_interface_input is not None:
        requested_interfaces.add(client_interface)
    if hotspot_interface_input is not None:
        requested_interfaces.add(hotspot_interface)

    if requested_interfaces:
        available = {adapter.interface for adapter in await _list_wifi_adapters()}
        missing = sorted(interface for interface in requested_interfaces if interface not in available)
        if missing:
            raise HTTPException(status_code=400, detail=f"Unknown Wi-Fi interface(s): {', '.join(missing)}")

    save_wifi_config(
        current.get("ssid"),
        bool(current.get("auto_connect", False)),
        current.get("band"),
        client_interface=client_interface,
        hotspot_interface=hotspot_interface,
    )

    if client_interface != current_client or hotspot_interface != current_hotspot:
        asyncio.create_task(_run_wifi_automanage(reason="interfaces-update", attempts=1, delay_seconds=0.0))

    return WifiInterfacesResponse(client_interface=client_interface, hotspot_interface=hotspot_interface)

@app.get("/wifi/scan", response_model=List[WifiNetwork], dependencies=[Depends(verify_token)])
async def scan_wifi():
    """
    Scans for available WiFi networks.
    """
    if not os.path.exists(WIFI_SCAN_SCRIPT_PATH):
         raise HTTPException(status_code=500, detail=f"WiFi scan script not found at {WIFI_SCAN_SCRIPT_PATH}")

    try:
        # Run the python script
        cmd = ["python3", WIFI_SCAN_SCRIPT_PATH]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            error_details = stderr.decode()
            print(f"WiFi scan error: {error_details}")
            # Try to return partial results or empty list? 
            # Or raise error. Let's raise error for now.
            raise Exception(f"Script failed with code {proc.returncode}: {error_details}")
            
        output = stdout.decode().strip()
        if not output:
             return []
        return json.loads(output)
        
    except Exception as e:
        import traceback
        traceback.print_exc() # Print full stack trace to logs
        print(f"WiFi scan failed: {e}")
        # Return 500
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/wifi/connect", response_model=JobResponse, dependencies=[Depends(verify_token)])
async def connect_wifi(request: WifiConnectRequest):
    """
    Connects to a WiFi network.
    This starts a background job to run the connection script.
    """
    request_client_interface = _validate_interface_name(request.client_interface, "client_interface")
    request_hotspot_interface = _validate_interface_name(request.hotspot_interface, "hotspot_interface")

    configured_client, configured_hotspot = _get_configured_wifi_interfaces()
    client_interface = request_client_interface or configured_client
    hotspot_interface = request_hotspot_interface or configured_hotspot

    if request_client_interface or request_hotspot_interface:
        available = {adapter.interface for adapter in await _list_wifi_adapters()}
        missing = sorted(
            iface for iface in {client_interface, hotspot_interface} if iface not in available
        )
        if missing:
            raise HTTPException(status_code=400, detail=f"Unknown Wi-Fi interface(s): {', '.join(missing)}")

    cmd = [
        "sudo", "-n", WIFI_CONNECT_SCRIPT_PATH,
        "--client-iface", client_interface,
        "--hotspot-iface", hotspot_interface,
        request.ssid,
        request.password or "",
    ]
    masked_cmd = [
        "sudo", "-n", WIFI_CONNECT_SCRIPT_PATH,
        "--client-iface", client_interface,
        "--hotspot-iface", hotspot_interface,
        request.ssid,
        "***" if request.password else "",
    ]
    
    # Translate band to nmcli format if present
    wifi_band = ""
    if request.band == "2.4GHz":
        wifi_band = "bg"
    elif request.band == "5GHz":
        wifi_band = "a"
        
    if wifi_band:
        cmd.append(wifi_band)
        masked_cmd.append(wifi_band)

    # Note: connect script now handles 3rd argument as BAND
    
    # If auto_connect is requested, save the config immediately
    if request.auto_connect:
        save_wifi_config(
            request.ssid,
            True,
            request.band,
            client_interface=client_interface,
            hotspot_interface=hotspot_interface,
        )
    
    # Check if script exists (only nice to have check, the job will fail if not found)
    # But locally on windows it's different path.
    
    job_id = await job_manager.start_job(cmd, display_command=" ".join(masked_cmd))
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not created")
        
    return JobResponse(
        jobId=job.id,
        status=job.status,
        exitCode=job.exit_code,
        startedAt=job.created_at,
        finishedAt=job.finished_at,
        command=job.command
    )


@app.post("/wifi/disable", response_model=JobResponse, dependencies=[Depends(verify_token)])
async def disable_wifi_and_enable_hotspot():
    """
    Disables Wi-Fi client usage by forcing hotspot mode.
    """
    client_interface, hotspot_interface = _get_configured_wifi_interfaces()
    cmd = [
        "sudo", "-n", WIFI_CONNECT_SCRIPT_PATH,
        "--hotspot",
        "--client-iface", client_interface,
        "--hotspot-iface", hotspot_interface,
    ]

    job_id = await job_manager.start_job(cmd)
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not created")

    return JobResponse(
        jobId=job.id,
        status=job.status,
        exitCode=job.exit_code,
        startedAt=job.created_at,
        finishedAt=job.finished_at,
        command=job.command,
    )


@app.get("/wifi/auto-connect", dependencies=[Depends(verify_token)])
async def get_wifi_auto_connect():
    return load_wifi_config()


@app.get("/wifi/hotspot/password", response_model=HotspotPasswordStatusResponse, dependencies=[Depends(verify_token)])
async def get_hotspot_password():
    config = load_hotspot_config()
    _, hotspot_interface = _get_configured_wifi_interfaces()
    supported_channels = await _read_hotspot_supported_channels(hotspot_interface)
    return HotspotPasswordStatusResponse(
        configured=(config["source"] == "configured"),
        source=config["source"],
        band=config.get("band"),
        channel=config.get("channel"),
        hotspotInterface=hotspot_interface,
        supportedChannels=supported_channels,
    )


@app.post("/wifi/hotspot/password", response_model=HotspotPasswordUpdateResponse, dependencies=[Depends(verify_token)])
async def set_hotspot_password(request: HotspotPasswordRequest):
    password = request.password.strip()
    if len(password) < 8 or len(password) > 63:
        raise HTTPException(status_code=400, detail="Hotspot password must be between 8 and 63 characters")

    normalized_band, normalized_channel = _validate_hotspot_band_channel(request.band, request.channel)

    client_interface, hotspot_interface = _get_configured_wifi_interfaces()
    supported_channels = await _read_hotspot_supported_channels(hotspot_interface)
    if normalized_channel is not None and normalized_band is not None:
        band_channels = supported_channels.get(normalized_band, [])
        if band_channels and normalized_channel not in band_channels:
            raise HTTPException(
                status_code=400,
                detail=f"Channel {normalized_channel} is not supported on {hotspot_interface} for {normalized_band}",
            )

    save_hotspot_settings(password=password, band=normalized_band, channel=normalized_channel)

    applied_now = False
    if await is_hotspot_active_on_interface(hotspot_interface):
        cmd = [
            "sudo", "-n", WIFI_CONNECT_SCRIPT_PATH,
            "--hotspot",
            "--client-iface", client_interface,
            "--hotspot-iface", hotspot_interface,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            error_text = stderr.decode(errors="replace").strip() or "unknown error"
            raise HTTPException(
                status_code=500,
                detail=f"Password saved, but failed to apply to active hotspot: {error_text}",
            )
        applied_now = True

    return HotspotPasswordUpdateResponse(
        status="success",
        message="Hotspot default password updated and applied" if applied_now else "Hotspot default password updated",
        configured=True,
        appliedToActiveHotspot=applied_now,
        band=normalized_band,
        channel=normalized_channel,
    )


@app.get("/wifi/hotspot/settings", response_model=HotspotPasswordStatusResponse, dependencies=[Depends(verify_token)])
async def get_hotspot_settings():
    return await get_hotspot_password()


@app.post("/wifi/hotspot/settings", response_model=HotspotPasswordUpdateResponse, dependencies=[Depends(verify_token)])
async def set_hotspot_settings(request: HotspotPasswordRequest):
    return await set_hotspot_password(request)


@app.post("/wifi/auto-connect", dependencies=[Depends(verify_token)])
async def set_wifi_auto_connect(config: WifiAutoConnectRequest):
    current = load_wifi_config()
    new_ssid = config.ssid if config.ssid else current.get("ssid")
    
    if config.auto_connect and not new_ssid:
        raise HTTPException(status_code=400, detail="SSID is required when enabling auto-connect")
        
    save_wifi_config(new_ssid, config.auto_connect, config.band)
    return {"status": "success", "message": "Wifi auto-connect configuration saved", "config": {"ssid": new_ssid, "auto_connect": config.auto_connect, "band": config.band}}


@app.get("/wifi/status", response_model=WifiStatusResponse, dependencies=[Depends(verify_token)])
async def get_wifi_status():
    """
    Check current WiFi connection status and SSID.
    """
    try:
        # Use nmcli to get active connections
        # We use 'device wifi' to get frequency information directly for the connected network
        cmd = ["nmcli", "-t", "-f", "IN-USE,SSID,FREQ", "device", "wifi"]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode().strip()
        
        connected_ssid = None
        band = None 
        
        if output:
            for line in output.split('\n'):
                # Format: *:SSID:Frequency MHz
                # Example: *:MyHomeWifi:5240 MHz
                # Clean up any potential escaping
                parts = line.split(':')
                
                # Check if this line is the "in-use" one (starts with *)
                if parts[0] == "*":
                    if len(parts) >= 3:
                        ssid = parts[1]
                        
                        # Filter out hotspot self-connection if needed
                        if ssid == "Hotspot" or ssid.startswith("pins-") or ssid == "hotspot-ap":
                             continue
                             
                        connected_ssid = ssid
                        freq_str = parts[2].replace(" MHz", "").strip()
                        
                        try:
                            freq = int(freq_str)
                            if 2400 <= freq <= 2500:
                                band = "2.4GHz"
                            elif 5000 <= freq <= 6000:
                                band = "5GHz"
                        except:
                            pass
                            
                    break
        
        return WifiStatusResponse(
            connected=bool(connected_ssid),
            ssid=connected_ssid,
            band=band
        )
        
    except Exception as e:
        print(f"Error checking wifi status: {e}")
        return WifiStatusResponse(connected=False, ssid=None, band=None)


class SystemTimeResponse(BaseModel):
    timestamp: float
    iso: str


@app.get("/system/temperature", response_model=PiTemperatureResponse, dependencies=[Depends(verify_token)])
async def get_system_temperature():
    """
    Get current Raspberry Pi temperature in Celsius and Fahrenheit.
    """
    # Try vcgencmd first (common on Raspberry Pi OS)
    try:
        proc = await asyncio.create_subprocess_exec(
            "vcgencmd", "measure_temp",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            output = stdout.decode().strip()
            # Expected format: temp=48.7'C
            match = re.search(r"temp=([0-9]+(?:\.[0-9]+)?)", output)
            if match:
                celsius = float(match.group(1))
                return PiTemperatureResponse(
                    celsius=celsius,
                    fahrenheit=(celsius * 9 / 5) + 32,
                    source="vcgencmd"
                )
    except Exception as e:
        print(f"vcgencmd temperature read failed: {e}")

    # Fallback: Linux thermal zone file (typically millidegrees C)
    try:
        thermal_path = "/sys/class/thermal/thermal_zone0/temp"
        with open(thermal_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        celsius = float(raw) / 1000.0
        return PiTemperatureResponse(
            celsius=celsius,
            fahrenheit=(celsius * 9 / 5) + 32,
            source="thermal_zone0"
        )
    except Exception as e:
        print(f"thermal_zone0 temperature read failed: {e}")

    raise HTTPException(status_code=500, detail="Unable to read system temperature")


@app.get("/system/time", response_model=SystemTimeResponse, dependencies=[Depends(verify_token)])
async def get_system_time():
    """
    Get the current system time.
    """
    now = datetime.now()
    return SystemTimeResponse(
        timestamp=now.timestamp(),
        iso=now.isoformat()
    )


@app.post("/system/time", response_model=JobResponse, dependencies=[Depends(verify_token)])
async def set_system_time(request: SystemTimeRequest):
    """
    Sets system timezone and system time using timedatectl (requires sudo).
    dateTime should be ISO-8601 and timezone should be an IANA timezone.
    """
    timezone_name = _validate_timezone_name(request.timezone)
    request_dt = _parse_request_datetime(request.dateTime)
    target_zone = ZoneInfo(timezone_name)

    if request_dt.tzinfo is None:
        target_dt = request_dt.replace(tzinfo=target_zone)
    else:
        target_dt = request_dt.astimezone(target_zone)

    # First, disable NTP (Automatic time synchronization) to avoid errors
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "timedatectl", "set-ntp", "false",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip() or "timedatectl set-ntp failed"
            raise HTTPException(status_code=500, detail=detail)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to disable NTP: {e}")

    # Set timezone before setting local wall time.
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "timedatectl", "set-timezone", timezone_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip() or "timedatectl set-timezone failed"
            raise HTTPException(status_code=500, detail=detail)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to set timezone: {e}")

    # Format datetime for timedatectl set-time in device local timezone.
    time_str = target_dt.strftime("%Y-%m-%d %H:%M:%S")
    
    # We use sudo timedatectl set-time "..."
    cmd = ["sudo", "-n", "timedatectl", "set-time", time_str]
    
    job_id = await job_manager.start_job(cmd)
    job = job_manager.get_job(job_id)

    return JobResponse(
        jobId=job.id,
        status=job.status,
        exitCode=job.exit_code,
        startedAt=job.created_at,
        finishedAt=job.finished_at,
        command=job.command
    )


class DhcpClient(BaseModel):
    ip: str
    mac: str
    hostname: Optional[str] = None
    expires: Optional[str] = None


class DhcpClientsResponse(BaseModel):
    clients: List[DhcpClient]


class DiagnosticsCollectRequest(BaseModel):
    includePinsJournal: bool = True
    includeApiJournal: bool = True
    includeUsb: bool = True
    includeDmesg: bool = True
    includeSystemInfo: bool = True
    includeNetworkInfo: bool = True
    includeKernelModules: bool = True
    journalLines: int = 2000
    dmesgLines: int = 4000


class DiagnosticsSectionOption(BaseModel):
    key: str
    label: str
    description: str
    defaultEnabled: bool


class DiagnosticsOptionsResponse(BaseModel):
    sections: List[DiagnosticsSectionOption]
    journalLinesDefault: int
    dmesgLinesDefault: int


class DiagnosticsArchiveStartResponse(BaseModel):
    archiveId: str
    status: str
    pollUrl: str
    downloadUrl: str


class DiagnosticsArchiveStatusResponse(BaseModel):
    archiveId: str
    status: str
    startedAt: float
    finishedAt: Optional[float] = None
    expiresAt: Optional[float] = None
    error: Optional[str] = None
    downloadUrl: Optional[str] = None


DIAGNOSTICS_ARCHIVE_JOBS: Dict[str, Dict[str, Any]] = {}


_DNSMASQ_LEASES_CANDIDATES = [
    "/var/lib/NetworkManager/dnsmasq-wlan0.leases",
    "/var/lib/NetworkManager/dnsmasq-wlan1.leases",
    "/var/lib/misc/dnsmasq.leases",
]
DNSMASQ_LEASES_FILE = os.getenv("DNSMASQ_LEASES_FILE", "")


def _leases_candidates() -> list[str]:
    if DNSMASQ_LEASES_FILE:
        return [DNSMASQ_LEASES_FILE]
    return list(_DNSMASQ_LEASES_CANDIDATES)


def _parse_leases_text(text: str) -> list[DhcpClient]:
    clients: list[DhcpClient] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) < 4:
            continue
        expiry_epoch, mac, ip = parts[0], parts[1], parts[2]
        hostname = parts[3] if parts[3] != "*" else None
        try:
            expires = datetime.fromtimestamp(int(expiry_epoch)).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError):
            expires = None
        clients.append(DhcpClient(ip=ip, mac=mac, hostname=hostname, expires=expires))
    return clients


@app.get("/wifi/clients", response_model=DhcpClientsResponse, dependencies=[Depends(verify_token)])
async def get_dhcp_clients():
    """/var/lib/NetworkManager/ is mode 700 so direct open() fails for non-root.
    Use 'sudo -n cat' for each candidate; the first that succeeds wins.
    Required sudoers rule (add to /etc/sudoers.d/pinsdaemon on the Pi):
      sysupdate-api ALL=(root) NOPASSWD: /usr/bin/cat /var/lib/NetworkManager/dnsmasq-wlan0.leases
    """
    errors: list[str] = []
    for leases_path in _leases_candidates():
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "cat", leases_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            reason = stderr.decode(errors="replace").strip() or f"exit code {proc.returncode}"
            errors.append(f"{leases_path}: {reason}")
            continue
        return DhcpClientsResponse(clients=_parse_leases_text(stdout.decode(errors="replace")))

    if errors:
        raise HTTPException(status_code=500, detail="Could not read any DHCP lease file: " + "; ".join(errors))
    return DhcpClientsResponse(clients=[])


def _cleanup_directory(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _diagnostics_download_url(archive_id: str) -> str:
    return f"/diagnostics/archive/{archive_id}/download"


def _prune_expired_diagnostics_jobs() -> None:
    now = time.time()
    expired_ids: list[str] = []
    for archive_id, job in DIAGNOSTICS_ARCHIVE_JOBS.items():
        expires_at = job.get("expiresAt")
        if isinstance(expires_at, (int, float)) and expires_at <= now:
            expired_ids.append(archive_id)

    for archive_id in expired_ids:
        job = DIAGNOSTICS_ARCHIVE_JOBS.pop(archive_id, None)
        if not job:
            continue
        work_dir = job.get("workDir")
        if isinstance(work_dir, str) and work_dir:
            _cleanup_directory(work_dir)


def _validate_diagnostics_request(request: DiagnosticsCollectRequest) -> None:
    if request.journalLines < 100 or request.journalLines > 50000:
        raise HTTPException(status_code=400, detail="journalLines must be between 100 and 50000")
    if request.dmesgLines < 100 or request.dmesgLines > 50000:
        raise HTTPException(status_code=400, detail="dmesgLines must be between 100 and 50000")

    if not any([
        request.includePinsJournal,
        request.includeApiJournal,
        request.includeUsb,
        request.includeDmesg,
        request.includeSystemInfo,
        request.includeNetworkInfo,
        request.includeKernelModules,
    ]):
        raise HTTPException(status_code=400, detail="At least one diagnostics section must be enabled")


def _diagnostics_status_response_from_job(job: Dict[str, Any]) -> DiagnosticsArchiveStatusResponse:
    status = str(job.get("status", "unknown"))
    archive_id = str(job.get("archiveId", ""))
    started_at = float(job.get("startedAt", time.time()))
    finished_at_raw = job.get("finishedAt")
    finished_at = float(finished_at_raw) if isinstance(finished_at_raw, (int, float)) else None
    expires_at_raw = job.get("expiresAt")
    expires_at = float(expires_at_raw) if isinstance(expires_at_raw, (int, float)) else None
    error = str(job.get("error")) if job.get("error") else None
    download_url = _diagnostics_download_url(archive_id) if status == "success" else None
    return DiagnosticsArchiveStatusResponse(
        archiveId=archive_id,
        status=status,
        startedAt=started_at,
        finishedAt=finished_at,
        expiresAt=expires_at,
        error=error,
        downloadUrl=download_url,
    )


async def _run_diagnostics_archive_job(archive_id: str) -> None:
    job = DIAGNOSTICS_ARCHIVE_JOBS.get(archive_id)
    if not job:
        return

    job["status"] = "running"
    raw_dir = job["rawDir"]
    command = job["command"]

    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=DIAGNOSTICS_COLLECTION_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        job["status"] = "failed"
        job["error"] = "Diagnostics collection timed out"
        job["finishedAt"] = time.time()
        job["expiresAt"] = time.time() + DIAGNOSTICS_RETENTION_SECONDS
        return
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = f"Diagnostics collection failed: {exc}"
        job["finishedAt"] = time.time()
        job["expiresAt"] = time.time() + DIAGNOSTICS_RETENTION_SECONDS
        return

    if proc.returncode != 0:
        error_text = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip() or "unknown error"
        job["status"] = "failed"
        job["error"] = f"Diagnostics collector returned {proc.returncode}: {error_text}"
        job["finishedAt"] = time.time()
        job["expiresAt"] = time.time() + DIAGNOSTICS_RETENTION_SECONDS
        return

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    archive_name = f"pins-diagnostics-{timestamp}.zip"
    archive_path = os.path.join(job["workDir"], archive_name)

    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for root, _, files in os.walk(raw_dir):
                for file_name in sorted(files):
                    source_path = os.path.join(root, file_name)
                    archive_path_name = os.path.relpath(source_path, raw_dir)
                    archive.write(source_path, archive_path_name)
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = f"Failed to build diagnostics archive: {exc}"
        job["finishedAt"] = time.time()
        job["expiresAt"] = time.time() + DIAGNOSTICS_RETENTION_SECONDS
        return

    job["archiveName"] = archive_name
    job["archivePath"] = archive_path
    job["status"] = "success"
    job["finishedAt"] = time.time()
    job["expiresAt"] = time.time() + DIAGNOSTICS_RETENTION_SECONDS


def _build_diagnostics_command(request: DiagnosticsCollectRequest, output_dir: str) -> list[str]:
    cmd = [
        "sudo", "-n", DIAGNOSTICS_SCRIPT_PATH,
        "--output-dir", output_dir,
        "--journal-lines", str(request.journalLines),
        "--dmesg-lines", str(request.dmesgLines),
    ]

    if not request.includePinsJournal:
        cmd.append("--no-pins-journal")
    if not request.includeApiJournal:
        cmd.append("--no-api-journal")
    if not request.includeUsb:
        cmd.append("--no-usb")
    if not request.includeDmesg:
        cmd.append("--no-dmesg")
    if not request.includeSystemInfo:
        cmd.append("--no-system-info")
    if not request.includeNetworkInfo:
        cmd.append("--no-network-info")
    if not request.includeKernelModules:
        cmd.append("--no-kernel-modules")

    return cmd


@app.get("/diagnostics/options", response_model=DiagnosticsOptionsResponse, dependencies=[Depends(verify_token)])
async def get_diagnostics_options():
    return DiagnosticsOptionsResponse(
        sections=[
            DiagnosticsSectionOption(
                key="includePinsJournal",
                label="PINS journal",
                description="Collects journalctl logs from pins service units",
                defaultEnabled=True,
            ),
            DiagnosticsSectionOption(
                key="includeApiJournal",
                label="API journal",
                description="Collects journalctl logs from sysupdate-api service",
                defaultEnabled=True,
            ),
            DiagnosticsSectionOption(
                key="includeUsb",
                label="USB device inventory",
                description="Collects lsusb and usb topology information",
                defaultEnabled=True,
            ),
            DiagnosticsSectionOption(
                key="includeDmesg",
                label="Kernel ring buffer",
                description="Collects dmesg output including USB-related kernel lines",
                defaultEnabled=True,
            ),
            DiagnosticsSectionOption(
                key="includeSystemInfo",
                label="System information",
                description="Collects date, uptime, OS info and selected service states",
                defaultEnabled=True,
            ),
            DiagnosticsSectionOption(
                key="includeNetworkInfo",
                label="Network information",
                description="Collects nmcli, ip and rfkill diagnostic information",
                defaultEnabled=True,
            ),
            DiagnosticsSectionOption(
                key="includeKernelModules",
                label="Kernel modules",
                description="Collects lsmod output",
                defaultEnabled=True,
            ),
        ],
        journalLinesDefault=2000,
        dmesgLinesDefault=4000,
    )


@app.post("/diagnostics/archive/start", response_model=DiagnosticsArchiveStartResponse, status_code=202, dependencies=[Depends(verify_token)])
async def start_diagnostics_archive(request: DiagnosticsCollectRequest):
    _validate_diagnostics_request(request)
    _prune_expired_diagnostics_jobs()

    os.makedirs(DIAGNOSTICS_WORK_DIR, exist_ok=True)
    archive_id = str(uuid.uuid4())
    work_dir = tempfile.mkdtemp(prefix=f"diag-{archive_id[:8]}-", dir=DIAGNOSTICS_WORK_DIR)
    raw_dir = os.path.join(work_dir, "raw")
    os.makedirs(raw_dir, exist_ok=True)

    command = _build_diagnostics_command(request, raw_dir)

    DIAGNOSTICS_ARCHIVE_JOBS[archive_id] = {
        "archiveId": archive_id,
        "status": "queued",
        "startedAt": time.time(),
        "finishedAt": None,
        "expiresAt": None,
        "error": None,
        "workDir": work_dir,
        "rawDir": raw_dir,
        "archivePath": None,
        "archiveName": None,
        "command": command,
    }

    asyncio.create_task(_run_diagnostics_archive_job(archive_id))

    return DiagnosticsArchiveStartResponse(
        archiveId=archive_id,
        status="queued",
        pollUrl=f"/diagnostics/archive/{archive_id}",
        downloadUrl=_diagnostics_download_url(archive_id),
    )


@app.post("/diagnostics/archive", response_model=DiagnosticsArchiveStartResponse, status_code=202, dependencies=[Depends(verify_token)])
async def create_diagnostics_archive(request: DiagnosticsCollectRequest):
    # Backward-compatible alias: now returns a start response instead of blocking until ZIP is ready.
    return await start_diagnostics_archive(request)


@app.get("/diagnostics/archive/{archive_id}", response_model=DiagnosticsArchiveStatusResponse, dependencies=[Depends(verify_token)])
async def get_diagnostics_archive_status(archive_id: str):
    _prune_expired_diagnostics_jobs()
    job = DIAGNOSTICS_ARCHIVE_JOBS.get(archive_id)
    if not job:
        raise HTTPException(status_code=404, detail="Diagnostics archive job not found")
    return _diagnostics_status_response_from_job(job)


@app.get("/diagnostics/archive/{archive_id}/download", dependencies=[Depends(verify_token)])
async def download_diagnostics_archive(archive_id: str):
    _prune_expired_diagnostics_jobs()
    job = DIAGNOSTICS_ARCHIVE_JOBS.get(archive_id)
    if not job:
        raise HTTPException(status_code=404, detail="Diagnostics archive job not found")

    status = job.get("status")
    if status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="Diagnostics archive is still being prepared")
    if status == "failed":
        raise HTTPException(status_code=500, detail=job.get("error") or "Diagnostics archive preparation failed")

    archive_path = job.get("archivePath")
    archive_name = job.get("archiveName")
    if not isinstance(archive_path, str) or not archive_path or not os.path.exists(archive_path):
        raise HTTPException(status_code=404, detail="Diagnostics archive file not found")

    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=archive_name or f"pins-diagnostics-{archive_id}.zip",
    )

