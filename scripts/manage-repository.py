#!/usr/bin/env python3
"""Select the PINS package channel without changing Debian/Raspberry Pi sources."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

REPOSITORY_URL = "https://repo.touch-n-stars.eu/reprepro"
CHANNELS = ("trixie", "unstable")
MANAGED_LIST = "pins-channel.list"
MANAGED_SOURCES = "pins-channel.sources"
PREFERENCE = "pins-channel.pref"


def is_pins_uri(uri):
    parsed = urlsplit(uri)
    return parsed.hostname == "repo.touch-n-stars.eu" and parsed.path.rstrip("/") == "/reprepro"


def source_paths(root):
    paths = [root / "etc/apt/sources.list"]
    directory = root / "etc/apt/sources.list.d"
    return [path for path in paths + sorted(directory.glob("*.list")) + sorted(directory.glob("*.sources"))
            if path.is_file()]


def fields(stanza):
    result = {}
    previous = None
    for line in stanza.splitlines():
        if line.startswith((" ", "\t")):
            if previous:
                result[previous] += " " + line.strip()
            continue
        match = re.match(r"([\w-]+):\s*(.*)", line)
        if match:
            previous = match[1].lower()
            result[previous] = match[2].strip()
    return result


def inspect_sources(root):
    """Return active PINS sources and untouched source text for precise edits."""
    entries = []
    for path in source_paths(root):
        text = path.read_text()
        if path.suffix == ".sources":
            for stanza in re.split(r"\n\s*\n", text):
                data = fields(stanza)
                uris = data.get("uris", "").split()
                if data.get("enabled", "yes").lower() == "no" or "deb" not in data.get("types", "").split():
                    continue
                if any(is_pins_uri(uri) for uri in uris):
                    if not all(is_pins_uri(uri) for uri in uris):
                        raise ValueError("PINS and other repositories share a deb822 stanza; separate them first")
                    suites = data.get("suites", "").split()
                    if not suites or any(suite not in CHANNELS for suite in suites):
                        raise ValueError("Unsupported PINS repository suite")
                    entries.append((path, stanza, data, suites))
        else:
            for line in text.splitlines():
                match = re.match(r"^(\s*deb\s+(?:\[[^\]]*\]\s+)?)(\S+)(\s+)(\S+)(.*)$", line)
                if match and is_pins_uri(match[2]):
                    if match[4] not in CHANNELS:
                        raise ValueError("Unsupported PINS repository suite")
                    entries.append((path, line, match, [match[4]]))
    return entries


def repository_status(root=Path("/")):
    entries = inspect_sources(root)
    suites = sorted({suite for _, _, _, names in entries for suite in names})
    channel = "unstable" if "unstable" in suites else "trixie" if "trixie" in suites else None
    return {"channel": channel, "configured": bool(entries), "suites": suites,
            "repositoryUrl": REPOSITORY_URL, "options": list(CHANNELS),
            "packagesUrl": f"{REPOSITORY_URL}/dists/{channel}/main/binary-arm64/Packages" if channel else None}


def atomic_write(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write(content)
    try:
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def configure_repository(channel, root=Path("/"), refresh=True):
    if channel not in CHANNELS:
        raise ValueError("Repository channel must be trixie or unstable")
    entries = inspect_sources(root)
    managed = {root / "etc/apt/sources.list.d" / name for name in (MANAGED_LIST, MANAGED_SOURCES)}
    base = [entry for entry in entries if entry[0] not in managed]
    if not base:
        raise ValueError("No existing PINS APT source; configure the signed trixie repository first")
    changes = {}
    # Preserve all existing trust and architecture settings. Keep trixie as the
    # dependency fallback, and add the experimental overlay only when selected.
    for path, stanza, data, _ in base:
        old = changes.get(path, path.read_text())
        if path.suffix == ".sources":
            stable = re.sub(r"(?im)^Suites:[^\n]*(?:\n[ \t]+[^\n]*)*", "Suites: trixie", stanza)
        else:
            stable = f"{data[1]}{data[2]}{data[3]}trixie{data[5]}"
        changes[path] = (old.replace(stanza, stable) if path.suffix == ".sources" else
                         re.sub("^" + re.escape(stanza) + "$", lambda _: stable, old, flags=re.MULTILINE))
    for path in managed:
        changes[path] = None
    if channel == "unstable":
        path, stanza, data, _ = base[0]
        if path.suffix == ".sources":
            overlay = re.sub(r"(?im)^Suites:[^\n]*(?:\n[ \t]+[^\n]*)*", "Suites: unstable", stanza)
            changes[root / "etc/apt/sources.list.d" / MANAGED_SOURCES] = overlay + "\n"
        else:
            overlay = f"{data[1]}{data[2]}{data[3]}unstable{data[5]}"
            changes[root / "etc/apt/sources.list.d" / MANAGED_LIST] = overlay + "\n"
    changes[root / "etc/apt/preferences.d" / PREFERENCE] = (
        "# Managed by PINS: select this channel even when its version is lower.\n"
        "Package: pins pinsdaemon pins-plugin-*\n"
        f"Pin: release o=Touch-N-Stars,n={channel}\n"
        "Pin-Priority: 1002\n\n"
        "Package: pins pinsdaemon pins-plugin-*\n"
        f"Pin: release o=Touch-N-Stars,n={'trixie' if channel == 'unstable' else 'unstable'}\n"
        "Pin-Priority: 1001\n"
    )
    originals = {}
    for path in changes:
        if path.is_symlink():
            raise ValueError(f"Refusing to replace a symlink: {path}")
        originals[path] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
    try:
        for path, content in changes.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, content.encode())
        if refresh:
            # A failed download/signature check must fail the switch, even when
            # APT would normally keep using its previous cached index.
            subprocess.run(["apt-get", "-o", "APT::Update::Error-Mode=any", "update"], check=True)
        return repository_status(root)
    except Exception:
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, *original)
        if refresh:
            subprocess.run(["apt-get", "-o", "APT::Update::Error-Mode=any", "update"], check=False)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("status", "set"))
    parser.add_argument("channel", nargs="?", choices=CHANNELS)
    args = parser.parse_args()
    if args.action == "status" and args.channel is None:
        print(json.dumps(repository_status()))
        return
    if args.action != "set" or args.channel is None:
        parser.error("Use status or set <trixie|unstable>")
    if os.geteuid() != 0:
        parser.error("Repository changes require root")
    with open("/run/lock/pins-repository.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another PINS package operation is running")
        print(f"Switching PINS repository to {args.channel}", flush=True)
        print(json.dumps(configure_repository(args.channel)), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
