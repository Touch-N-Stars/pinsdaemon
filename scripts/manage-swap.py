#!/usr/bin/python3
"""Read or persist swap sizing. Never resize, swapoff, or reboot a live host."""
import argparse
import configparser
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import tempfile

MIB = 1024 * 1024
SIZES = (2, 4, 8)
DROPIN = "99-pinsdaemon-swap.conf"


def system_path(root, path):
    return Path(root) / path.lstrip("/")


def rpi_config(root):
    # Follow systemd main-file and drop-in precedence; /etc wins same-name ties.
    directories = ["/etc", "/run", "/usr/local/lib", "/usr/lib"]
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.optionxform = str
    for directory in directories:
        path = system_path(root, directory + "/rpi/swap.conf")
        if path.exists():
            config.read(path, encoding="utf-8")
            break
    snippets = {}
    for directory in reversed(directories):
        for path in system_path(root, directory + "/rpi/swap.conf.d").glob("*.conf"):
            snippets[path.name] = path
    for name in sorted(snippets):
        config.read(snippets[name], encoding="utf-8")
    return config


def dphys_config(root):
    path = system_path(root, "/etc/dphys-swapfile")
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s*(CONF_[A-Z]+)\s*=\s*(.*?)\s*$", line)
            if match:
                tokens = shlex.split(match[2], comments=True)
                if len(tokens) == 1:
                    values[match[1]] = tokens[0]
    return values


def active_swap(root, file_name):
    path = system_path(root, "/proc/swaps")
    if not path.exists():
        return None, None
    total_kib = file_kib = 0
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 5:
            raise ValueError("Invalid active swap inventory.")
        size = int(fields[2])
        total_kib += size
        if fields[0] == file_name:
            file_kib += size
    # mkswap reserves a header page; round up to report the configured MiB.
    return ((total_kib * 1024 + MIB - 1) // MIB,
            (file_kib * 1024 + MIB - 1) // MIB)


def swap_status(root="/"):
    backend = None
    mechanism = None
    configured = None
    file_name = "/var/swap"
    reason = "No supported swap manager found."
    generators = ("/usr/lib/systemd/system-generators/rpi-swap-generator",
                  "/lib/systemd/system-generators/rpi-swap-generator")
    if any(system_path(root, path).exists() for path in generators):
        backend = "rpi-swap"
        config = rpi_config(root)
        mechanism = config.get("Main", "Mechanism", fallback="auto").strip()
        if mechanism == "auto":
            mechanism = "zram+file"
        file_name = config.get("File", "Path", fallback="/var/swap").strip()
        value = config.get("File", "FixedSizeMiB", fallback="").strip()
        configured = int(value) if value else None
        supported = mechanism in {"swapfile", "zram+file"}
        reason = "This swap mechanism does not use a configurable swap file."
    elif any(system_path(root, path).exists() for path in
             ("/sbin/dphys-swapfile", "/usr/sbin/dphys-swapfile")):
        backend = mechanism = "dphys-swapfile"
        config = dphys_config(root)
        file_name = config.get("CONF_SWAPFILE", "/var/swap")
        configured = int(config["CONF_SWAPSIZE"]) if config.get("CONF_SWAPSIZE") else None
        supported = True
    else:
        supported = False
    if not file_name.startswith("/") or ".." in Path(file_name).parts:
        raise ValueError("Unsupported swap file path.")
    path = system_path(root, file_name)
    if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.stat().st_mode)):
        raise ValueError("Swap file must be a regular file, not a link or device.")
    active = path.stat().st_size // MIB if path.exists() else 0
    parent = path.parent
    if not parent.is_dir():
        supported = False
        reason = "Swap file directory is unavailable."
    available = shutil.disk_usage(parent).free if parent.is_dir() else 0
    # Without a fixed override, report the existing file; the image default is 2 GiB.
    configured = configured if configured is not None else (active or 2048)
    active_capacity, active_file_capacity = active_swap(root, file_name)
    pending = supported and configured != active
    if supported and mechanism in {"swapfile", "dphys-swapfile"}:
        pending = pending or active_file_capacity != configured
    return {
        "supported": supported, "backend": backend, "mechanism": mechanism,
        "configuredSizeMb": configured, "activeFileSizeMb": active,
        "activeSwapSizeMb": active_capacity,
        "availableBytes": available, "pendingReboot": pending,
        "optionsGb": list(SIZES), "defaultSizeGb": 2,
        "unsupportedReason": None if supported else reason,
    }


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Refusing to replace a linked configuration file.")
    fd, temporary = tempfile.mkstemp(prefix=".pins-swap-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.chmod(temporary, 0o644)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure_swap(size_gb, root="/"):
    if type(size_gb) is not int or size_gb not in SIZES:
        raise ValueError("Swap size must be 2, 4, or 8 GB.")
    status = swap_status(root)
    if not status["supported"]:
        raise ValueError(status["unsupportedReason"])
    size_mb = size_gb * 1024
    growth = max(0, size_mb - status["activeFileSizeMb"]) * MIB
    if growth and status["availableBytes"] < growth + 256 * MIB:
        raise ValueError("Not enough free storage; leave at least 256 MiB after growing swap.")
    if status["backend"] == "rpi-swap":
        path = system_path(root, "/etc/rpi/swap.conf.d/" + DROPIN)
        text = ("# Managed by pinsdaemon; applied on next boot.\n"
                "[Main]\nMechanism=swapfile\n\n[File]\n"
                f"FixedSizeMiB={size_mb}\nMaxSizeMiB={size_mb}\n")
    else:
        path = system_path(root, "/etc/dphys-swapfile")
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        text = "\n".join(line for line in original.splitlines()
                         if not re.match(r"^\s*CONF_(SWAPSIZE|MAXSWAP)\s*=", line))
        text += f"\nCONF_SWAPSIZE={size_mb}\nCONF_MAXSWAP={size_mb}\n"
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    atomic_write(path, text)
    try:
        result = swap_status(root)
        if result["configuredSizeMb"] != size_mb:
            raise ValueError("Another swap configuration overrides this setting.")
        if result["backend"] == "rpi-swap" and result["mechanism"] != "swapfile":
            raise ValueError("Another swap configuration overrides the swapfile mechanism.")
        return result
    except Exception:
        if previous is None:
            path.unlink()
        else:
            atomic_write(path, previous)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    setter = commands.add_parser("set")
    setter.add_argument("size_gb", type=int, choices=SIZES)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run through the pinsdaemon privileged helper.")
    try:
        # Serialize writes across daemon workers/processes without disabling swap.
        import fcntl
        fd = os.open("/run/lock/pinsdaemon-swap.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = swap_status() if args.command == "status" else configure_swap(args.size_gb)
        print(json.dumps(result))
    except (OSError, ValueError, configparser.Error) as error:
        message = "_".join(str(error).split())
        print(f"PINS_SWAP_RESULT code=SWAP_CONFIGURATION_FAILED message={message}", flush=True)
        parser.exit(1, f"Swap configuration failed: {error}\n")


if __name__ == "__main__":
    main()
