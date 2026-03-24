#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -lt 2 ]]; then
    echo "Usage: $0 <download_url> <asset_name>"
    exit 1
fi

DOWNLOAD_URL="$1"
ASSET_NAME="$2"

if [[ "$DOWNLOAD_URL" != https://github.com/acocalypso/indi3rdparty/releases/download/latest-build/*.deb ]]; then
    echo "Refusing to download from untrusted URL: $DOWNLOAD_URL"
    exit 1
fi

if [[ "$ASSET_NAME" != *.deb ]]; then
    echo "Only .deb assets are supported"
    exit 1
fi

normalized_name=$(echo "$ASSET_NAME" | tr '[:upper:]' '[:lower:]')
if [[ "$normalized_name" == *dbgsym* || "$normalized_name" == *-dbg_* || "$normalized_name" == *_dbg_* || "$normalized_name" == *-dbg.deb || "$normalized_name" == *_dbg.deb ]]; then
    echo "Refusing debug package: $ASSET_NAME"
    exit 1
fi

WORK_DIR=$(mktemp -d /tmp/pins-indi3rdparty-XXXXXX)
cleanup() {
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

TARGET_PATH="$WORK_DIR/$ASSET_NAME"

echo "Downloading $ASSET_NAME..."
python3 - "$DOWNLOAD_URL" "$TARGET_PATH" <<'PY'
import sys
import urllib.request

url = sys.argv[1]
out = sys.argv[2]

req = urllib.request.Request(url, headers={"User-Agent": "pinsdaemon-indi-installer/1.0"})
with urllib.request.urlopen(req, timeout=30) as resp, open(out, "wb") as f:
    while True:
        chunk = resp.read(1024 * 1024)
        if not chunk:
            break
        f.write(chunk)
PY

if [[ ! -s "$TARGET_PATH" ]]; then
    echo "Download failed or produced empty file"
    exit 1
fi

echo "Installing $ASSET_NAME..."
if ! dpkg -i "$TARGET_PATH"; then
    echo "Resolving dependencies..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -f -y
    dpkg -i "$TARGET_PATH"
fi

echo "Installation completed: $ASSET_NAME"
