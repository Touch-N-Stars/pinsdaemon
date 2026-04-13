import os
import json
import csv
import asyncio
import uuid
import re
import fnmatch
import urllib.request
import urllib.error
from datetime import datetime
from fastapi import FastAPI, Depends, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

from .auth import verify_token
from .job_manager import job_manager, JobStatus
from .wifi_config import load_wifi_config, save_wifi_config
from .hotspot_config import load_hotspot_config, save_hotspot_password

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
WIFI_CONNECT_SCRIPT_PATH = os.getenv("WIFI_CONNECT_SCRIPT_PATH", "/usr/local/bin/wifi-connect.sh")
FIRMWARE_INSTALL_SCRIPT_PATH = os.getenv("FIRMWARE_INSTALL_SCRIPT_PATH", "/usr/local/bin/install-firmware.sh")
INDI_INSTALL_SCRIPT_PATH = os.getenv("INDI_INSTALL_SCRIPT_PATH", "/usr/local/bin/install-indi-package.sh")
FIRMWARE_STATE_FILE = os.getenv("FIRMWARE_STATE_FILE", "/opt/pinsdaemon/firmware.txt")
FIRMWARE_UPLOAD_DIR = os.getenv("FIRMWARE_UPLOAD_DIR", "/tmp/pinsdaemon-firmware")
FIRMWARE_ZIP_RE = re.compile(r"^firmware_(\d{8})_(\d{6})\.zip$", re.IGNORECASE)
INDI_RELEASE_API_URL = os.getenv(
    "INDI_RELEASE_API_URL",
    "https://api.github.com/repos/acocalypso/indi3rdparty/releases/tags/latest-build",
)
UPDATES_PACKAGES_URL = os.getenv(
    "UPDATES_PACKAGES_URL",
    "https://repo.touch-n-stars.eu/reprepro/dists/trixie/main/binary-arm64/Packages",
)
UPDATES_PACKAGE_PATTERNS = [
    p.strip() for p in os.getenv("UPDATES_PACKAGE_PATTERNS", "pins,pinsdaemon,pins-plugin-*").split(",") if p.strip()
]
UPGRADE_LAST_JOB_FILE = os.getenv("UPGRADE_LAST_JOB_FILE", "/opt/pinsdaemon/last-upgrade-job.json")

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

class HotspotPasswordStatusResponse(BaseModel):
    configured: bool
    source: str

class HotspotPasswordUpdateResponse(BaseModel):
    status: str
    message: str
    configured: bool
    appliedToActiveHotspot: bool


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
    assetName: str


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

class SystemTimeRequest(BaseModel):
    timestamp: float

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


@app.post("/packages/indi3rdparty/install", response_model=JobResponse, dependencies=[Depends(verify_token)])
async def install_indi3rdparty_package(request: IndiPackageInstallRequest):
    target_asset = request.assetName.strip()
    if not target_asset:
        raise HTTPException(status_code=400, detail="assetName is required")

    try:
        packages = await _build_indi_packages(only_not_installed=False, name_filter=None)
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch release metadata: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to build package list: {e}")

    selected = next((p for p in packages if p.assetName == target_asset), None)
    if not selected:
        raise HTTPException(status_code=404, detail="Selected package asset not found in latest-build release")

    cmd = ["sudo", "-n", INDI_INSTALL_SCRIPT_PATH, selected.downloadUrl, selected.assetName]
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
    return HotspotPasswordStatusResponse(configured=(config["source"] == "configured"), source=config["source"])


@app.post("/wifi/hotspot/password", response_model=HotspotPasswordUpdateResponse, dependencies=[Depends(verify_token)])
async def set_hotspot_password(request: HotspotPasswordRequest):
    password = request.password.strip()
    if len(password) < 8 or len(password) > 63:
        raise HTTPException(status_code=400, detail="Hotspot password must be between 8 and 63 characters")

    save_hotspot_password(password)

    applied_now = False
    client_interface, hotspot_interface = _get_configured_wifi_interfaces()
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
    )


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
    Sets the system time using timedatectl (requires sudo).
    The timestamp should be a float (Unix epoch).
    """
    # First, disable NTP (Automatic time synchronization) to avoid errors
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "timedatectl", "set-ntp", "false",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.wait()
    except Exception as e:
        print(f"Error disabling NTP: {e}")

    # Convert timestamp to format expected by timedatectl: "YYYY-MM-DD HH:MM:SS"
    dt = datetime.fromtimestamp(request.timestamp)
    time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
    
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

