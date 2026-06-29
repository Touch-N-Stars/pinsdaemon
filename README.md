# PINS Daemon (System Management API)

A lightweight, secure, Python-based daemon designed for the Raspberry Pi to expose system management capabilities via a REST API. It handles system updates, firmware installation, Samba share management, PHD2 service control, Wi-Fi configuration, and system telemetry/time operations.

## Features

- **System Updates**: Trigger `apt update && apt upgrade` remotely.
- **Firmware Management**: Upload versioned firmware archives and install contained `.deb` packages asynchronously.
- **ASTAP Star Database Management**: List and install supported ASTAP star databases (D50, D05, G05, W08) for Raspberry Pi 64-bit.
- **Samba Management**: Enable or disable SMB shares for file access.
- **PHD2 Management**: Check and control `phd2` service state.
- **Wi-Fi Management**: Scan for available networks, connect securely, configure auto-connect, and inspect current connection status.
- **Wi-Fi Runtime Recovery**: Retries reconnect when wlan0 drops and falls back to device hotspot after repeated failures.
- **System Utilities**: Read Pi temperature, read system time, and set system time.
- **Secure Architecture**:
  - Runs as a restricted user (`sysupdate-api`).
  - No shell injection: Commands are hard-coded or strictly parameterized.
  - Privileges delegated via `sudoers` (no root API access).
  - Bearer Token authentication.
- **Real-time Feedback**: WebSocket endpoint for streaming command execution logs.

## High-Level Design

```mermaid
graph TD
  Client[API Client] -->|HTTP + Bearer Token| API[FastAPI Daemon]
  Client -->|WebSocket logs| WS[WebSocket logs endpoint]
  WS --> API

  subgraph "System Service (sysupdate-api)"
    API
    Startup[Startup Task]
    JobMgr[Job Manager]
  end

  API -->|On startup| Startup
  Startup -->|sudo -n| EnsureReq[ensure-required-packages.sh]

  API -->|Async jobs| JobMgr

  subgraph "Privileged Operations (sudo -n)"
    Upgrade[system-upgrade.sh]
    Firmware[install-firmware.sh]
    Indi[install-indi-package.sh]
    Astap[install-astap-star-database.sh]
    Plugin[manage-plugin.sh]
    Samba[manage-samba.sh]
    WifiConnect[wifi-connect.sh]
    SysCtl[systemctl / timedatectl / cat leases]
  end

  JobMgr --> Upgrade
  JobMgr --> Firmware
  JobMgr --> Indi
  JobMgr --> Astap
  JobMgr --> Plugin
  JobMgr --> Samba
  JobMgr --> WifiConnect
  JobMgr --> SysCtl

  API -->|python3| WifiScan[wifi-scan.py]
  WifiScan -->|sudo iwlist| Radio[iwlist / nmcli]

  API -->|Repo metadata| Repo[(APT Packages index)]
  API -->|Release metadata| GitHub[(GitHub Releases API)]

  NM[NetworkManager Dispatcher] --> WifiRecovery[90-pins-wifi-recovery]
  WifiRecovery -->|Reconnect / fallback| WifiConnect
```

The daemon provides a facade over system shell scripts. Long-running tasks (like upgrades or Wi-Fi connections) are executed asynchronously as "Jobs". Clients receive a `Job ID` immediately and can use it to poll status or stream logs via WebSockets.

## API Endpoints

All HTTP endpoints require the `Authorization: Bearer <token>` header.

### 1. System Upgrade

Triggers a system package upgrade.

- **URL**: `POST /upgrade`
- **Body**:
  ```json
  {
    "dryRun": false
  }
  ```
- **Response**: `JobResponse` object.

### 2. Firmware Upload

Upload a firmware archive and trigger background installation if it is newer than installed firmware.

- **URL**: `POST /firmware/upload`
- **Body**: `multipart/form-data` with file field `file`.
- **Filename format**: `firmware_DDMMYYYY_HHMMSS.zip`
- **Response**: `FirmwareUploadResponse` object.
  Example (installation started):
  ```json
  {
    "status": "started",
    "message": "Firmware upload complete. Installation started.",
    "firmwareTag": "firmware_17032026_153000",
    "currentFirmwareTag": "firmware_16032026_101500",
    "job": {
      "jobId": "uuid-string",
      "status": "started",
      "exitCode": null,
      "startedAt": 1710689400.0,
      "finishedAt": null,
      "command": "sudo -n /usr/local/bin/install-firmware.sh ..."
    }
  }
  ```
  Example (already up to date):
  ```json
  {
    "status": "up_to_date",
    "message": "Firmware is already up to date",
    "firmwareTag": "firmware_17032026_153000",
    "currentFirmwareTag": "firmware_17032026_153000",
    "job": null
  }
  ```

### 3. Samba Management

Check or toggle the file sharing service.

- **URL**: `GET /samba`
- **Response**:
  ```json
  {
    "enabled": true
  }
  ```

Enable or disable the file sharing service.

- **URL**: `POST /samba`
- **Body**:
  ```json
  {
    "enable": true
  }
  ```
- **Response**: `JobResponse` object.

### 4. PHD2 Service Management

Check or toggle `phd2` state.

- **URL**: `GET /phd2`
- **Response**: `{ "enabled": true|false, "running": true|false }`

- **URL**: `POST /phd2`
- **Body**:
  ```json
  {
    "enable": true
  }
  ```
- **Response**: `JobResponse` object.

### 5. Wi-Fi Scan

Get a list of available Wi-Fi networks.

- **URL**: `GET /wifi/scan`
- **Response**: List of network objects.
  ```json
  [
    {
      "ssid": "MyWiFi",
      "signal_strength": -55,
      "quality": "60/70",
      "encrypted": true,
      "channel": 6,
      "frequency": 2.437,
      "mac": "00:11:22:33:44:55"
    }
  ]
  ```

### 6. Wi-Fi Connect

Connect to a specific Wi-Fi network. If connection fails, it automatically reverts to Hotspot mode.

Runtime behavior:
- A NetworkManager dispatcher hook monitors disconnect/change events on the configured client/hotspot interfaces.
- It retries reconnection to the configured auto-connect SSID (default: 3 attempts, 5s backoff).
- If retries fail, it enables the device hotspot automatically.
- The fallback hotspot profile uses a higher NetworkManager autoconnect priority than default client Wi-Fi profiles so the device remains reachable instead of bouncing between a flaky client network and hotspot mode.

- **URL**: `POST /wifi/connect`
- **Body**:
  ```json
  {
    "ssid": "MyWiFi",
    "password": "secretpassword",
    "auto_connect": true,
    "band": "2.4GHz"
  }
  ```
- **Response**: `JobResponse` object.

### 7. Wi-Fi Disable (Force Hotspot)

Disable Wi-Fi client mode and force hotspot mode.

- **URL**: `POST /wifi/disable`
- **Response**: `JobResponse` object.

### 8. Wi-Fi Auto-Connect

- **URL**: `GET /wifi/auto-connect`
- **Response**:
  ```json
  {
    "ssid": "MyWiFi",
    "auto_connect": true,
    "band": "2.4GHz"
  }
  ```

- **URL**: `POST /wifi/auto-connect`
- **Body**:
  ```json
  {
    "ssid": "MyWiFi",
    "auto_connect": true,
    "band": "2.4GHz"
  }
  ```

### 9. Wi-Fi Status

Return whether device is connected to Wi-Fi and detect active band.

- **URL**: `GET /wifi/status`
- **Response**:
  ```json
  {
    "connected": true,
    "ssid": "MyWiFi",
    "band": "5GHz"
  }
  ```

### 10. Hotspot Password

Get hotspot configuration status without exposing the password value.

- **URL**: `GET /wifi/hotspot/password`
- **Response**:
  ```json
  {
    "configured": true,
    "source": "configured",
    "band": "2.4GHz",
    "channel": 6,
    "hotspotInterface": "wlan1",
    "supportedChannels": {
      "2.4GHz": [1, 6, 11],
      "5GHz": [36, 40, 44, 48]
    }
  }
  ```

Alias endpoint for the same payload:

- **URL**: `GET /wifi/hotspot/settings`

Update hotspot settings used by hotspot mode.

- **URL**: `POST /wifi/hotspot/password`
- **Body**:
  ```json
  {
    "password": "newstrongpass",
    "band": "5GHz",
    "channel": 44
  }
  ```
- **Response**:
  ```json
  {
    "status": "success",
    "message": "Hotspot default password updated",
    "configured": true,
    "appliedToActiveHotspot": false,
    "band": "5GHz",
    "channel": 44
  }
  ```
- **Validation**: password must be 8-63 characters. `band` accepts `2.4GHz` or `5GHz` (also aliases `bg`/`a`). `channel` accepts any positive integer and is checked against adapter-reported supported channels when available.

Alias endpoint for the same update behavior:

- **URL**: `POST /wifi/hotspot/settings`

### 11. System Temperature

- **URL**: `GET /system/temperature`
- **Response**:
  ```json
  {
    "celsius": 48.7,
    "fahrenheit": 119.66,
    "source": "vcgencmd"
  }
  ```

### 12. System Time

- **URL**: `GET /system/time`
- **Response**:
  ```json
  {
    "timestamp": 1710000000.0,
    "iso": "2026-03-17T12:00:00"
  }
  ```

- **URL**: `POST /system/time`
- **Body**:
  ```json
  {
    "dateTime": "2026-05-06T21:30:00+02:00",
    "timezone": "Europe/Berlin"
  }
  ```
- **Response**: `JobResponse` object.

### Diagnostics Archive

Expose selectable troubleshooting bundles for GUI checkboxes and support team debugging.

- **URL**: `GET /diagnostics/options`
- **Response**:
  ```json
  {
    "sections": [
      {
        "key": "includePinsJournal",
        "label": "PINS journal",
        "description": "Collects journalctl logs from pins service units",
        "defaultEnabled": true
      },
      {
        "key": "includeUsb",
        "label": "USB device inventory",
        "description": "Collects lsusb and usb topology information",
        "defaultEnabled": true
      }
    ],
    "journalLinesDefault": 2000,
    "dmesgLinesDefault": 4000
  }
  ```

- **URL**: `POST /diagnostics/archive/start`
- **Body**:
  ```json
  {
    "includePinsJournal": true,
    "includeApiJournal": true,
    "includeUsb": true,
    "includeDmesg": true,
    "includeSystemInfo": true,
    "includeNetworkInfo": true,
    "includeKernelModules": true,
    "journalLines": 2000,
    "dmesgLines": 4000
  }
  ```
- **Response** (`202 Accepted`):
  ```json
  {
    "archiveId": "e6f96b6d-6f45-4c38-81c4-f778d2af8d83",
    "status": "queued",
    "pollUrl": "/diagnostics/archive/e6f96b6d-6f45-4c38-81c4-f778d2af8d83",
    "downloadUrl": "/diagnostics/archive/e6f96b6d-6f45-4c38-81c4-f778d2af8d83/download"
  }
  ```

Backward-compatible alias (same start behavior):

- **URL**: `POST /diagnostics/archive`

Poll status:

- **URL**: `GET /diagnostics/archive/{archiveId}`
- **Response**:
  ```json
  {
    "archiveId": "e6f96b6d-6f45-4c38-81c4-f778d2af8d83",
    "status": "running",
    "startedAt": 1760000000.0,
    "finishedAt": null,
    "expiresAt": null,
    "error": null,
    "downloadUrl": null
  }
  ```

Download archive when status is `success`:

- **URL**: `GET /diagnostics/archive/{archiveId}/download`
- **Response**: `application/zip` file download (`pins-diagnostics-YYYYMMDD_HHMMSS.zip`)

The ZIP contains selected troubleshooting data such as:

- `journalctl -u pins` and `journalctl -u sysupdate-api` logs
- local pinsdaemon daily log files from `/opt/pinsdaemon/logs` retained for 5 days, including daemon output, job output, and Wi-Fi recovery decisions
- `lsusb`, `lsusb -t`, `usb-devices`
- `dmesg` tail and USB-focused dmesg filters
- `nmcli`, `ip`, `rfkill`, and `iw` outputs
- basic system details (`uname`, `os-release`, `timedatectl`, service status)

### 13. Check Updates

Check whether updates are available for a whitelist of relevant packages.
The daemon reads installed versions locally and compares them against the configured APT Packages index.

- **URL**: `GET /updates/check`
- **Response**:
  ```json
  {
    "hasUpdates": true,
    "checkedAt": "2026-03-21T21:40:00Z",
    "packages": [
      {
        "name": "pins",
        "installedVersion": "3.3.0.1019-nightly+173",
        "latestVersion": "3.3.0.1020-nightly+174",
        "updateAvailable": true
      },
      {
        "name": "pinsdaemon",
        "installedVersion": "1.0.0-173",
        "latestVersion": "1.0.1-174",
        "updateAvailable": true
      }
    ]
  }
  ```

- **Environment variables**:
  - `UPDATES_PACKAGES_URL` (default: `https://repo.touch-n-stars.eu/reprepro/dists/trixie/main/binary-arm64/Packages`)
  - `UPDATES_PACKAGE_PATTERNS` (default: `pins,pinsdaemon,pins-plugin-*`)

### 14. Indi3rdparty Packages

List available packages from the latest GitHub release of:
`https://github.com/acocalypso/indi3rdparty/releases/latest`

- Debug packages are excluded (`dbg`/`dbgsym` variants).
- Supports filtering to only packages not currently installed.

- **URL**: `GET /packages/indi3rdparty`
- **Query params**:
  - `onlyNotInstalled` (optional bool, default `false`)
  - `q` (optional string filter by package/asset name)
- **Response**:
  ```json
  {
    "checkedAt": "2026-03-24T20:10:00Z",
    "onlyNotInstalled": true,
    "packages": [
      {
        "name": "indi-some-driver",
        "assetName": "indi-some-driver_1.2.3_arm64.deb",
        "version": "1.2.3",
        "architecture": "arm64",
        "downloadUrl": "https://github.com/acocalypso/indi3rdparty/releases/download/indi3rdparty-v2.1.9-14/indi-some-driver_1.2.3_arm64.deb",
        "installed": false,
        "installedVersion": null
      }
    ]
  }
  ```

Install a selected package from the same release.

- **URL**: `POST /packages/indi3rdparty/install`
- **Body**:
  ```json
  {
    "assetName": "indi-some-driver_1.2.3_arm64.deb"
  }
  ```
- **Response**: `JobResponse` object.

Read current INDI 3rdparty registry (`3rdparty.json`) including all entries grouped by type.

- **URL**: `GET /packages/indi3rdparty/registry`
- **Response**:
  ```json
  {
    "updatedAt": "2026-06-12T10:30:00Z",
    "totalEntries": 4,
    "entriesByType": {
      "camera": [
        {"Name": "indi_asi_ccd", "Label": "ASI CCD", "Type": "camera"}
      ],
      "filterwheel": [],
      "flatpanel": [],
      "focuser": [],
      "rotator": [],
      "switches": [
        {"Name": "indi_something_switch", "Label": "SOMETHING SWITCH", "Type": "switches"}
      ],
      "telescope": [],
      "weather": []
    }
  }
  ```

Edit one registry entry by driver name. You can rename it (`Name`), relabel (`Label`), and/or move it to another type bucket (`Type`).

- **URL**: `PATCH /packages/indi3rdparty/registry/{entryName}`
- **Body** (all fields optional):
  ```json
  {
    "Name": "indi_asi_ccd",
    "Label": "ASI Camera",
    "Type": "camera"
  }
  ```
- **Response**: full `Indi3rdpartyRegistryResponse` object after the update.

- **Environment variables**:
  - `INDI_RELEASE_API_URL` (default: `https://api.github.com/repos/acocalypso/indi3rdparty/releases/latest`)
  - `INDI_INSTALL_SCRIPT_PATH` (default: `/usr/local/bin/install-indi-package.sh`)
  - `INDI_3RDPARTY_JSON_PATH` (default: `/home/pi/Documents/INDI/3rdparty.json`)

### 15. ASTAP Star Databases

List installable ASTAP star databases for Raspberry Pi 64-bit.
Supported selections: `D50`, `D05`, `G05`, `W08`.

- **URL**: `GET /packages/astap/stardatabases`
- **Query params**:
  - `onlyNotInstalled` (optional bool, default `true`)
  - `q` (optional string filter by database id/label)
- **Response**:
  ```json
  {
    "checkedAt": "2026-06-09T12:00:00Z",
    "onlyNotInstalled": true,
    "packages": [
      {
        "databaseId": "D50",
        "label": "D50",
        "description": "Large star database",
        "downloadUrl": "https://sourceforge.net/projects/astap-program/files/star_databases/d50_star_database.deb/download",
        "installed": false,
        "installedPackage": null,
        "installedVersion": null
      }
    ]
  }
  ```

Install one ASTAP star database.

- **URL**: `POST /packages/astap/stardatabases/install`
- **Body**:
  ```json
  {
    "databaseId": "D50"
  }
  ```
- **Response**: `JobResponse` object.

- **Environment variables**:
  - `ASTAP_STAR_DATABASE_INSTALL_SCRIPT_PATH` (default: `/usr/local/bin/install-astap-star-database.sh`)
  - `ASTAP_STAR_DATABASE_STATE_FILE` (default: `/opt/pinsdaemon/astap-star-databases.json`)

### 16. Job Status

Check the status of a background job.

- **URL**: `GET /jobs/{jobId}`
- **Response**: `JobResponse` object.

### 17. Job Logs (WebSocket)

Stream live logs from a running job.

- **URL**: `ws://<host>:8000/logs/{jobId}?token=<token>`
- **Output**: Real-time text stream of stdout/stderr.

---

## Data Models

**JobResponse**
```json
{
  "jobId": "uuid-string",
  "status": "started|running|success|failed",
  "exitCode": null,
  "startedAt": 1678900000.0,
  "finishedAt": null,
  "command": "sudo ..." 
}
```

**FirmwareUploadResponse**
```json
{
  "status": "started|up_to_date",
  "message": "string",
  "firmwareTag": "firmware_DDMMYYYY_HHMMSS",
  "currentFirmwareTag": "firmware_DDMMYYYY_HHMMSS|null",
  "job": "JobResponse|null"
}
```

**SambaStatus**
```json
{
  "enabled": true
}
```

**Phd2Status**
```json
{
  "enabled": true,
  "running": true
}
```

**WifiStatusResponse**
```json
{
  "connected": true,
  "ssid": "MyWiFi",
  "band": "2.4GHz|5GHz|null"
}
```

**SystemTimeResponse**
```json
{
  "timestamp": 1710000000.0,
  "iso": "2026-03-17T12:00:00"
}
```

**PiTemperatureResponse**
```json
{
  "celsius": 48.7,
  "fahrenheit": 119.66,
  "source": "vcgencmd|thermal_zone0"
}
```

## Installation

### From Debian Package (Recommended on Pi)

1.  Download the latest `.deb` release.
2.  Install:
    ```bash
    sudo apt update
    sudo apt install ./pinsdaemon_*_arm64.deb
    ```
3.  The service `sysupdate-api` starts automatically.

### Upgrade Behavior (Debian Package)

When installing a newer `pinsdaemon` `.deb`, package hooks perform the following service sequence automatically:

1. Stop `pins` (if running).
2. Stop `gvfs-gphoto2-volume-monitor.service`.
3. Start `pins` again after installation finishes.

### Manual / Development Setup

1.  **Prerequisites**: Python 3.9+, `venv`.
2.  **User Setup**:
    ```bash
    sudo useradd -r -s /bin/false sysupdate-api
    ```
3.  **Deploy Code**: Copy `app/` and `scripts/` to `/opt/pinsdaemon`.
4.  **Install Deps**:
    ```bash
    python3 -m venv venv
    ./venv/bin/pip install -r requirements.txt
    ```
5.  **Configure Sudoers**: Copy `packaging/sudoers` content to `/etc/sudoers.d/sysupdate-api`.
6.  **Run**:
    ```bash
    sudo /opt/pinsdaemon/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
    ```
