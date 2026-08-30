#!/usr/bin/env bash
set -euo pipefail

DATABASE_ID_RAW="${1:-}"

if [[ -z "$DATABASE_ID_RAW" ]]; then
    echo "Usage: $0 <D50|D05|G05|W08>"
    exit 1
fi

DATABASE_ID="$(echo "$DATABASE_ID_RAW" | tr '[:lower:]' '[:upper:]')"

case "$DATABASE_ID" in
    D50)
        DOWNLOAD_URL="https://sourceforge.net/projects/astap-program/files/star_databases/d50_star_database.deb/download"
        ;;
    D05)
        DOWNLOAD_URL="https://sourceforge.net/projects/astap-program/files/star_databases/d05_star_database.deb/download"
        ;;
    G05)
        DOWNLOAD_URL="https://sourceforge.net/projects/astap-program/files/star_databases/g05_star_database.deb/download"
        ;;
    W08)
        DOWNLOAD_URL="https://sourceforge.net/projects/astap-program/files/star_databases/w08_star_database_mag08_astap.deb/download"
        ;;
    *)
        echo "Unsupported ASTAP star database: $DATABASE_ID_RAW"
        echo "Allowed values: D50, D05, G05, W08"
        exit 1
        ;;
esac

WORK_DIR="$(mktemp -d /tmp/pins-astap-db-XXXXXX)"
cleanup() {
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

FILE_NAME="$(basename "${DOWNLOAD_URL%/download}")"
if [[ -z "$FILE_NAME" || "$FILE_NAME" == "download" ]]; then
    FILE_NAME="${DATABASE_ID,,}_star_database.deb"
fi
TARGET_PATH="$WORK_DIR/$FILE_NAME"

STATE_FILE="${ASTAP_STAR_DATABASE_STATE_FILE:-/opt/pinsdaemon/astap-star-databases.json}"

echo "Downloading ASTAP star database ${DATABASE_ID}..."
echo "PINS_PROGRESS phase=downloading percent=0 bytes=0 total=0"
python3 - "$DOWNLOAD_URL" "$TARGET_PATH" <<'PY'
import socket
import sys
import time
import urllib.error
import urllib.request

url = sys.argv[1]
out = sys.argv[2]

chunk_size = 1024 * 1024
last_report_at = 0.0

for attempt in range(1, 4):
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "pinsdaemon-astap-db-installer/1.1"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp, open(out, "wb") as f:
            total_header = resp.headers.get("Content-Length", "")
            total = int(total_header) if total_header.isdigit() else 0
            downloaded = 0
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                if now - last_report_at >= 1 or (total and downloaded >= total):
                    percent = min(100, downloaded * 100 // total) if total else 0
                    print(
                        f"PINS_PROGRESS phase=downloading percent={percent} "
                        f"bytes={downloaded} total={total}",
                        flush=True,
                    )
                    last_report_at = now
        if downloaded <= 0:
            raise OSError("download produced no data")
        break
    except (OSError, TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        if attempt == 3:
            raise SystemExit(f"ASTAP database download failed after 3 attempts: {exc}")
        print(f"Download attempt {attempt} failed: {exc}. Retrying...", flush=True)
        time.sleep(attempt * 2)
else:
    raise SystemExit("ASTAP database download failed")
PY

if [[ ! -s "$TARGET_PATH" ]]; then
    echo "Download failed or produced an empty file"
    exit 1
fi

PACKAGE_NAME="$(dpkg-deb -f "$TARGET_PATH" Package 2>/dev/null || true)"
if [[ -z "$PACKAGE_NAME" ]]; then
    echo "Failed to read Debian package metadata from downloaded file"
    exit 1
fi

if dpkg-query -W -f='${Status}' "$PACKAGE_NAME" 2>/dev/null | grep -q "install ok installed"; then
    echo "Package already installed: $PACKAGE_NAME"
else
    echo "Installing package $PACKAGE_NAME..."
    echo "PINS_PROGRESS phase=installing percent=0 bytes=0 total=0"
    # ASTAP database packages share a small acknowledgement text file. Debian
    # treats that intentional overlap as a conflict unless overwrite is enabled.
    if ! dpkg --force-overwrite -i "$TARGET_PATH"; then
        echo "Resolving dependencies..."
        export DEBIAN_FRONTEND=noninteractive
        apt-get install -f -y
        dpkg --force-overwrite -i "$TARGET_PATH"
    fi
fi

echo "Updating ASTAP install state at $STATE_FILE..."
echo "PINS_PROGRESS phase=finalizing percent=0 bytes=0 total=0"
python3 - "$STATE_FILE" "$DATABASE_ID" "$PACKAGE_NAME" "$DOWNLOAD_URL" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

state_file = sys.argv[1]
database_id = sys.argv[2]
package_name = sys.argv[3]
download_url = sys.argv[4]

state = {}
if os.path.exists(state_file):
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            state = loaded
    except Exception:
        state = {}

databases = state.get("databases")
if not isinstance(databases, dict):
    databases = {}

updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
databases[database_id] = {
    "packageName": package_name,
    "downloadUrl": download_url,
    "updatedAt": updated_at,
}
state["databases"] = databases

state_dir = os.path.dirname(state_file)
if state_dir:
    os.makedirs(state_dir, exist_ok=True)

tmp_file = f"{state_file}.tmp"
with open(tmp_file, "w", encoding="utf-8") as f:
    json.dump(state, f, indent=2)
    f.write("\n")
os.replace(tmp_file, state_file)
PY

LINK_SCRIPT="/usr/local/bin/ensure-astap-data-links.sh"
if [[ ! -x "$LINK_SCRIPT" ]]; then
    LINK_SCRIPT="$(dirname "$0")/ensure-astap-data-links.sh"
fi

if [[ -x "$LINK_SCRIPT" ]]; then
    echo "Making ASTAP database files visible to the command-line application..."
    "$LINK_SCRIPT"
else
    echo "Warning: ASTAP data-link helper was not found; links will be repaired when pinsdaemon next starts."
fi

echo "ASTAP star database ${DATABASE_ID} is ready."
echo "PINS_PROGRESS phase=complete percent=100 bytes=0 total=0"
