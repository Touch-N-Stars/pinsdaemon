# PINS Daemon (System Management API)

A lightweight, secure, Python-based daemon designed for the Raspberry Pi to expose system management capabilities via a REST API. It handles system updates, firmware installation, Samba share management, PHD2 service control, Wi-Fi configuration, and system telemetry/time operations.

## Features

- **System Updates**: Trigger `apt update && apt upgrade` remotely.
- **Firmware Management**: Upload versioned firmware archives and install contained `.deb` packages asynchronously.
- **Samba Management**: Enable or disable SMB shares for file access.
- **PHD2 Management**: Check and control `phd2` service state.
- **Wi-Fi Management**: Scan for available networks, connect securely, configure auto-connect, and inspect current connection status.
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
    Client[API Client] -->|HTTP/WS + Token| API[FastAPI Daemon]
    
    subgraph "System Service (sysupdate-api)"
        API
        JobMgr[Job Manager]
    end
    
    API -->|Async| JobMgr
    
    subgraph "Privileged Operations (sudo)"
        Script1[system-upgrade.sh]
        Script2[manage-samba.sh]
        Script3[wifi-connect.sh]
        Script4[iwlist / nmcli]
    end
    
    JobMgr -->|sudo -n| Script1
    JobMgr -->|sudo -n| Script2
    JobMgr -->|sudo -n| Script3
    
    API -->|python3| Script5[wifi-scan.py]
    Script5 -->|sudo| Script4
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

### 7. Wi-Fi Auto-Connect

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

### 8. Wi-Fi Status

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

### 9. System Temperature

- **URL**: `GET /system/temperature`
- **Response**:
  ```json
  {
    "celsius": 48.7,
    "fahrenheit": 119.66,
    "source": "vcgencmd"
  }
  ```

### 10. System Time

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
    "timestamp": 1710000000.0
  }
  ```
- **Response**: `JobResponse` object.

### 11. Job Status

Check the status of a background job.

- **URL**: `GET /jobs/{jobId}`
- **Response**: `JobResponse` object.

### 12. Job Logs (WebSocket)

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
