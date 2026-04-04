import os
import subprocess

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
                
        entries.sort(key=lambda x: x["name"].lower())

        return entries

    except Exception:
        return []

def create_directory(path: str, name: str):
    if not path or not name:
        raise ValueError("Invalid path or name")

    if name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("Invalid directory name")

    base_path = os.path.abspath(path)
    new_path = os.path.abspath(os.path.join(base_path, name))

    try:
        if os.path.commonpath([base_path, new_path]) != base_path:
            raise ValueError("Invalid directory path")
    except ValueError:
        # Happens when paths are on different drives (Windows dev) or malformed.
        raise ValueError("Invalid directory path")

    if os.path.exists(new_path):
        raise ValueError("Directory already exists")

    try:
        os.makedirs(new_path)
    except PermissionError as e:
        # On Pi, removable media mounts can be owned by root/pi, so use constrained sudo fallback.
        if not (new_path.startswith("/media/") or new_path.startswith("/home/")):
            raise PermissionError(f"Permission denied: {new_path}") from e

        proc = subprocess.run(
            ["sudo", "-n", "/bin/mkdir", "-p", new_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "sudo mkdir failed").strip()
            raise PermissionError(f"Permission denied: {detail}") from e

    return {
        "name": name,
        "path": new_path
    }