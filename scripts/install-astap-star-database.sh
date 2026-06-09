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
python3 - "$DOWNLOAD_URL" "$TARGET_PATH" <<'PY'
import sys
import urllib.request

url = sys.argv[1]
out = sys.argv[2]

req = urllib.request.Request(url, headers={"User-Agent": "pinsdaemon-astap-db-installer/1.0"})
with urllib.request.urlopen(req, timeout=60) as resp, open(out, "wb") as f:
    while True:
        chunk = resp.read(1024 * 1024)
        if not chunk:
            break
        f.write(chunk)
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
    if ! dpkg -i "$TARGET_PATH"; then
        echo "Resolving dependencies..."
        export DEBIAN_FRONTEND=noninteractive
        apt-get install -f -y
        dpkg -i "$TARGET_PATH"
    fi
fi

echo "Updating ASTAP install state at $STATE_FILE..."
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

echo "ASTAP star database ${DATABASE_ID} is ready."
