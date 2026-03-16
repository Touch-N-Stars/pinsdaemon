#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -lt 2 ]]; then
    echo "Usage: $0 <firmware_zip_path> <firmware_tag> [firmware_state_file]"
    exit 1
fi

ZIP_PATH="$1"
FIRMWARE_TAG="$2"
FIRMWARE_STATE_FILE="${3:-/opt/pinsdaemon/firmware.txt}"

if [[ ! -f "$ZIP_PATH" ]]; then
    echo "Firmware archive not found: $ZIP_PATH"
    exit 1
fi

WORK_DIR=$(mktemp -d /tmp/pins-firmware-XXXXXX)
cleanup() {
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

echo "Starting firmware installation..."
echo "Archive: $ZIP_PATH"

echo "Extracting archive..."
unzip -o "$ZIP_PATH" -d "$WORK_DIR"

mapfile -t DEB_FILES < <(find "$WORK_DIR" -type f -name "*.deb" | sort)
if [[ "${#DEB_FILES[@]}" -eq 0 ]]; then
    echo "No .deb packages found in firmware archive."
    exit 1
fi

echo "Found ${#DEB_FILES[@]} package(s)."
echo "Installing packages via dpkg..."
dpkg -i "${DEB_FILES[@]}"

echo "Writing firmware state to $FIRMWARE_STATE_FILE"
printf "%s\n" "$FIRMWARE_TAG" > "$FIRMWARE_STATE_FILE"
chmod 644 "$FIRMWARE_STATE_FILE" || true

if rm -f "$ZIP_PATH"; then
    echo "Removed uploaded firmware archive."
fi

echo "Firmware installation completed successfully."
