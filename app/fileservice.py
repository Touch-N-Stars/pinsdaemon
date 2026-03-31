import asyncio
import json
import os
import shutil
import codecs

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "file_config.json")


# =========================
# USB Devices
# =========================

def get_devices():
    devices = []

    devices.append({
        "name": "Root",
        "path": "/"
    })

    try:
        with open("/proc/mounts", "r") as f:
            lines = f.readlines()

        seen = set()

        for line in lines:
            parts = line.split()
            mountpoint = parts[1]

            # Oktal-Escape-Sequenzen dekodieren (\040 ? Leerzeichen, \011 ? Tab, etc.)
            mountpoint = mountpoint.encode('raw_unicode_escape').decode('unicode_escape')

            if mountpoint.startswith("/media") or mountpoint.startswith("/mnt"):
                if mountpoint not in seen:
                    seen.add(mountpoint)
                    devices.append({
                        "name": mountpoint.split("/")[-1],
                        "path": mountpoint
                    })

    except Exception:
        pass

    return devices


# =========================
# Save Path Config
# =========================

def list_directories(path: str):
    if not path or not os.path.exists(path):
        return []

    try:
        entries = []

        for entry in os.listdir(path):
            full_path = os.path.join(path, entry)

            if os.path.isdir(full_path):
                entries.append({
                    "name": entry,
                    "path": full_path
                })

        # optional: sortieren (nice UX)
        entries.sort(key=lambda x: x["name"].lower())

        return entries

    except Exception:
        return []

def create_directory(path: str, name: str):
    if not path or not name:
        raise ValueError("Invalid path or name")

    new_path = os.path.join(path, name)

    if os.path.exists(new_path):
        raise ValueError("Directory already exists")

    os.makedirs(new_path)

    return {
        "name": name,
        "path": new_path
    }