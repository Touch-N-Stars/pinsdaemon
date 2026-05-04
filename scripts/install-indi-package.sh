#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -lt 2 ]]; then
    echo "Usage: $0 <download_url> <asset_name> [--type <type>] [--label <label>]"
    exit 1
fi

DOWNLOAD_URL="$1"
ASSET_NAME="$2"
shift 2

ENTRY_TYPE=""
ENTRY_LABEL=""
INDI_3RDPARTY_JSON_PATH="${INDI_3RDPARTY_JSON_PATH:-/home/pi/Documents/INDI/3rdparty.json}"

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --type)
            ENTRY_TYPE="${2:-}"
            if [[ -z "$ENTRY_TYPE" ]]; then
                echo "--type requires a value"
                exit 1
            fi
            shift 2
            ;;
        --label)
            ENTRY_LABEL="${2:-}"
            if [[ -z "$ENTRY_LABEL" ]]; then
                echo "--label requires a value"
                exit 1
            fi
            shift 2
            ;;
        *)
            echo "Unknown argument: $1"
            exit 1
            ;;
    esac
done

if [[ ! "$DOWNLOAD_URL" =~ ^https://github\.com/acocalypso/indi3rdparty/releases/download/[^/]+/[^/]+\.deb$ ]]; then
    echo "Refusing to download from untrusted URL: $DOWNLOAD_URL"
    exit 1
fi

if [[ "$ASSET_NAME" != *.deb ]]; then
    echo "Only .deb assets are supported"
    exit 1
fi

URL_BASENAME="${DOWNLOAD_URL##*/}"
if [[ "$URL_BASENAME" != "$ASSET_NAME" ]]; then
    echo "Refusing asset name mismatch between URL and requested asset"
    exit 1
fi

normalized_name=$(echo "$ASSET_NAME" | tr '[:upper:]' '[:lower:]')
if [[ "$normalized_name" == *dbgsym* || "$normalized_name" == *-dbg_* || "$normalized_name" == *_dbg_* || "$normalized_name" == *-dbg.deb || "$normalized_name" == *_dbg.deb ]]; then
    echo "Refusing debug package: $ASSET_NAME"
    exit 1
fi

PACKAGE_NAME="${ASSET_NAME%%_*}"
if [[ -z "$PACKAGE_NAME" ]]; then
    echo "Unable to determine package name from asset: $ASSET_NAME"
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

echo "Updating INDI 3rdparty registry at $INDI_3RDPARTY_JSON_PATH..."
if ! python3 - "$INDI_3RDPARTY_JSON_PATH" "$PACKAGE_NAME" "$ENTRY_TYPE" "$ENTRY_LABEL" <<'PY'
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

json_path = sys.argv[1]
package_name = sys.argv[2]
entry_type = (sys.argv[3] or "").strip()
entry_label = (sys.argv[4] or "").strip()

allowed_types = {
    "filterwheel",
    "flatpanel",
    "focuser",
    "rotator",
    "switches",
    "telescope",
    "weather",
}

aliases = {
    "filterwheels": "filterwheel",
    "flatpanels": "flatpanel",
    "focusers": "focuser",
    "rotators": "rotator",
    "switch": "switches",
    "telescopes": "telescope",
}


def normalize_type(value: str) -> str:
    candidate = aliases.get(value.strip().lower(), value.strip().lower())
    if candidate not in allowed_types:
        raise ValueError(
            f"Invalid type '{value}'. Allowed: {', '.join(sorted(allowed_types))}"
        )
    return candidate


def type_from_group(group_raw: str) -> str:
    g = (group_raw or "").strip().lower()
    g = g.replace("-", " ").replace("_", " ")

    if "filter" in g:
        return "filterwheel"
    if "flat" in g or "light" in g or "dust" in g:
        return "flatpanel"
    if "focuser" in g:
        return "focuser"
    if "rotator" in g:
        return "rotator"
    if "weather" in g:
        return "weather"
    if "telescope" in g or "mount" in g:
        return "telescope"
    return "switches"


def type_from_driver_name(driver_name: str) -> str:
    n = driver_name.strip().lower()

    if any(token in n for token in ("filter", "wheel")):
        return "filterwheel"
    if any(token in n for token in ("flat", "dust", "lightbox", "light")):
        return "flatpanel"
    if "focuser" in n or "focus" in n:
        return "focuser"
    if "rotator" in n:
        return "rotator"
    if "weather" in n or "meteo" in n:
        return "weather"
    if any(
        token in n
        for token in (
            "lx200",
            "mount",
            "telescope",
            "synscan",
            "nexstar",
            "temma",
            "gemini",
            "ioptron",
            "onstep",
            "starbook",
            "eq",
            "pmc8",
            "teenastro",
        )
    ):
        return "telescope"
    return "switches"


def label_from_driver_name(driver_name: str) -> str:
    base = driver_name.strip()
    if base.startswith("indi_"):
        base = base[5:]
    base = base.strip("_")
    if not base:
        return driver_name
    if "_" in base:
        return " ".join(part.upper() for part in base.split("_") if part)
    return base.upper()


def list_package_files(pkg: str) -> list[str]:
    proc = subprocess.run(
        ["dpkg-query", "-L", pkg],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []

    result: list[str] = []
    for line in proc.stdout.splitlines():
        path = line.strip()
        if path and os.path.exists(path):
            result.append(path)
    return sorted(set(result))


def list_package_xml_files(pkg_files: list[str]) -> list[str]:
    result: list[str] = []
    for path in pkg_files:
        if path.endswith(".xml") and "/usr/share/indi/" in path:
            result.append(path)
    return sorted(set(result))


def list_package_driver_binaries(pkg_files: list[str]) -> list[str]:
    known_non_driver_bins = {
        "indi_eval",
        "indi_getprop",
        "indi_setprop",
    }

    result: list[str] = []
    for path in pkg_files:
        basename = os.path.basename(path)
        if not basename.startswith("indi_"):
            continue
        if basename in known_non_driver_bins:
            continue
        if "." in basename:
            continue
        if not os.path.isfile(path):
            continue
        if not os.access(path, os.X_OK):
            continue
        result.append(path)
    return sorted(set(result))


def discover_drivers(pkg: str) -> tuple[list[tuple[str, str, str]], list[str], list[str]]:
    pkg_files = list_package_files(pkg)
    discovered_from_xml: list[tuple[str, str, str]] = []
    discovered_from_bins: list[tuple[str, str, str]] = []
    xml_files = list_package_xml_files(pkg_files)
    bin_files = list_package_driver_binaries(pkg_files)

    for xml_path in xml_files:
        try:
            root = ET.parse(xml_path).getroot()
        except Exception:
            continue

        found_in_group = False
        for group_node in root.findall(".//devGroup"):
            group = type_from_group(group_node.get("group", ""))
            for device in group_node.findall(".//device"):
                name = (device.get("driver") or device.get("name") or "").strip()
                label = (device.get("label") or name).strip()
                if name:
                    discovered_from_xml.append((name, label if label else name, group))
                    found_in_group = True

        if found_in_group:
            continue

        for device in root.findall(".//device"):
            name = (device.get("driver") or device.get("name") or "").strip()
            label = (device.get("label") or name).strip()
            if name:
                discovered_from_xml.append((name, label if label else name, "switches"))

    for bin_path in bin_files:
        name = os.path.basename(bin_path).strip()
        if not name:
            continue
        discovered_from_bins.append((name, label_from_driver_name(name), type_from_driver_name(name)))

    merged_by_name: dict[str, tuple[str, str, str]] = {}
    for name, label, dtype in discovered_from_bins:
        merged_by_name[name] = (name, label, dtype)

    # Prefer XML metadata (label/type) whenever present.
    for name, label, dtype in discovered_from_xml:
        merged_by_name[name] = (name, label, dtype)

    xml_names = sorted({name for name, _, _ in discovered_from_xml})
    bin_names = sorted({name for name, _, _ in discovered_from_bins})
    bin_only_names = [name for name in bin_names if name not in set(xml_names)]

    merged_entries = [merged_by_name[name] for name in sorted(merged_by_name.keys())]
    return merged_entries, xml_names, bin_only_names


def summarize_names(names: list[str], max_items: int = 20) -> str:
    if not names:
        return "-"
    if len(names) <= max_items:
        return ", ".join(names)
    return ", ".join(names[:max_items]) + f", ... (+{len(names) - max_items} more)"


if entry_type:
    entry_type = normalize_type(entry_type)

entries, xml_driver_names, bin_only_driver_names = discover_drivers(package_name)
if not entries:
    print(
        "Warning: no INDI drivers discovered from installed package; "
        "registry file was not modified.",
        file=sys.stderr,
    )
    raise SystemExit(0)

print(
    "Driver discovery summary: "
    f"xml={len(xml_driver_names)}, "
    f"binary_fallback_only={len(bin_only_driver_names)}"
)
print(f"  XML drivers: {summarize_names(xml_driver_names)}")
print(f"  Binary fallback drivers: {summarize_names(bin_only_driver_names)}")

if entry_type:
    entries = [(name, label, entry_type) for name, label, _ in entries]

if entry_label and len(entries) == 1:
    name, _, dtype = entries[0]
    entries = [(name, entry_label, dtype)]

default_data = {
    "filterwheel": [],
    "flatpanel": [],
    "focuser": [],
    "rotator": [],
    "switches": [],
    "telescope": [],
    "weather": [],
}

data = dict(default_data)
if os.path.exists(json_path):
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            for k in default_data.keys():
                v = loaded.get(k, [])
                data[k] = v if isinstance(v, list) else []
    except Exception:
        pass

for name, label, dtype in entries:
    entry = {"Name": name, "Label": label, "Type": dtype}
    bucket = data.setdefault(dtype, [])
    updated = False
    for idx, existing in enumerate(bucket):
        if isinstance(existing, dict) and existing.get("Name") == name:
            bucket[idx] = entry
            updated = True
            break
    if not updated:
        bucket.append(entry)

os.makedirs(os.path.dirname(json_path), exist_ok=True)
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")

print(f"Updated INDI 3rdparty registry with {len(entries)} entr(y/ies)")
PY
then
    echo "Warning: failed to update INDI 3rdparty registry metadata."
fi

echo "Installation completed: $ASSET_NAME"
