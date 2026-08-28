"""Verified Raspberry Pi Wi-Fi regulatory-country persistence."""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Sequence


COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
REGDOM_TOKEN_RE = re.compile(
    r"(?<!\S)cfg80211\.ieee80211_regdom=([^\s]+)(?=\s|$)"
)
DEFAULT_ISO3166_PATH = Path("/usr/share/zoneinfo/iso3166.tab")
DEFAULT_RASPI_CONFIG = "/usr/bin/raspi-config"
DEFAULT_IW = "/usr/sbin/iw"
CMDLINE_CANDIDATES = (
    Path("/boot/firmware/cmdline.txt"),
    Path("/boot/cmdline.txt"),
)


class RegulatoryError(RuntimeError):
    pass


class RegulatoryState(NamedTuple):
    configured: Optional[str]
    boot: Optional[str]
    runtime: Optional[str]
    consistent: bool


def normalize_country(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip().upper()
    return candidate if COUNTRY_RE.fullmatch(candidate) else None


def validate_supported_country(country: str, iso3166_path: Path) -> str:
    normalized = normalize_country(country)
    if normalized is None:
        raise RegulatoryError("invalid Wi-Fi country syntax")
    try:
        supported = {
            line.split(None, 1)[0]
            for line in iso3166_path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and len(line.split(None, 1)) == 2
        }
    except OSError as exc:
        raise RegulatoryError("Wi-Fi country database is unavailable") from exc
    if normalized not in supported:
        raise RegulatoryError("unsupported Wi-Fi country")
    return normalized


def find_cmdline_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit)
    for candidate in CMDLINE_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise RegulatoryError("Raspberry Pi kernel command line is unavailable")


def read_boot_countries(path: Path) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegulatoryError("cannot read Raspberry Pi kernel command line") from exc
    return [match.group(1).upper() for match in REGDOM_TOKEN_RE.finditer(content)]


def _split_cmdline(content: str) -> tuple[str, str]:
    if content.endswith("\r\n"):
        body, newline = content[:-2], "\r\n"
    elif content.endswith("\n"):
        body, newline = content[:-1], "\n"
    else:
        body, newline = content, ""
    if "\n" in body or "\r" in body:
        raise RegulatoryError("Raspberry Pi kernel command line must contain one line")
    if not body.strip():
        raise RegulatoryError("Raspberry Pi kernel command line is empty")
    return body, newline


def reconcile_boot_country(path: Path, country: str) -> bool:
    normalized = normalize_country(country)
    if normalized is None:
        raise RegulatoryError("invalid Wi-Fi country syntax")
    try:
        original = path.read_text(encoding="utf-8")
        metadata = path.stat()
    except OSError as exc:
        raise RegulatoryError("cannot read Raspberry Pi kernel command line") from exc

    body, newline = _split_cmdline(original)
    replacement = f"cfg80211.ieee80211_regdom={normalized}"
    matches = list(REGDOM_TOKEN_RE.finditer(body))
    if matches:
        pieces: list[str] = []
        cursor = 0
        for index, match in enumerate(matches):
            pieces.append(body[cursor : match.start()])
            if index == 0:
                pieces.append(replacement)
            cursor = match.end()
        pieces.append(body[cursor:])
        updated_body = "".join(pieces)
    else:
        separator = "" if body[-1].isspace() else " "
        updated_body = f"{body}{separator}{replacement}"
    updated = f"{updated_body}{newline}"
    if updated == original:
        return False

    temporary_name: Optional[str] = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.pins-", dir=str(path.parent)
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), stat.S_IMODE(metadata.st_mode))
            if hasattr(os, "fchown"):
                os.fchown(handle.fileno(), metadata.st_uid, metadata.st_gid)
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some boot filesystems do not support syncing a directory handle.
            pass
    except OSError as exc:
        raise RegulatoryError("cannot update Raspberry Pi kernel command line") from exc
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
    return True


def parse_runtime_country(output: str) -> Optional[str]:
    in_global = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line == "global":
            in_global = True
            continue
        if in_global:
            match = re.fullmatch(r"country\s+([A-Z0-9]{2}):.*", line)
            if match:
                return normalize_country(match.group(1))
            if line.startswith("phy#"):
                break
    return None


def _invoke(
    argv: Sequence[str],
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]],
    timeout: int = 20,
    environment: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    try:
        options = {
            "capture_output": True,
            "text": True,
            "timeout": timeout,
            "check": False,
        }
        if environment is not None:
            options["env"] = environment
        return run_command(list(argv), **options)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RegulatoryError(f"command unavailable: {Path(argv[0]).name}") from exc


def _configured_country(
    raspi_config: str, run_command: Callable[..., subprocess.CompletedProcess[str]]
) -> Optional[str]:
    result = _invoke(
        [raspi_config, "nonint", "get_wifi_country"],
        run_command=run_command,
        environment=_raspi_config_environment(),
    )
    if result.returncode != 0:
        return None
    return normalize_country(result.stdout)


def _runtime_country(
    iw_command: str, run_command: Callable[..., subprocess.CompletedProcess[str]]
) -> Optional[str]:
    result = _invoke([iw_command, "reg", "get"], run_command=run_command)
    if result.returncode != 0:
        return None
    return parse_runtime_country(result.stdout)


def _raspi_config_environment() -> dict[str, str]:
    """Prevent raspi-config from targeting the daemon user's missing session bus."""
    environment = os.environ.copy()
    for name in ("SUDO_USER", "SUDO_UID", "SUDO_GID", "SUDO_COMMAND"):
        environment.pop(name, None)
    return environment


def read_state(
    cmdline_path: Path,
    *,
    raspi_config: str = DEFAULT_RASPI_CONFIG,
    iw_command: str = DEFAULT_IW,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> RegulatoryState:
    configured = _configured_country(raspi_config, run_command)
    boot_values = read_boot_countries(cmdline_path)
    boot = boot_values[0] if len(boot_values) == 1 else None
    runtime = _runtime_country(iw_command, run_command)
    consistent = bool(
        configured
        and boot == configured
        and runtime == configured
        and len(boot_values) == 1
    )
    return RegulatoryState(configured, boot, runtime, consistent)


def apply_country(
    country: str,
    cmdline_path: Path,
    *,
    raspi_config: str = DEFAULT_RASPI_CONFIG,
    iw_command: str = DEFAULT_IW,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda _message: None,
) -> RegulatoryState:
    desired = normalize_country(country)
    if desired is None:
        raise RegulatoryError("invalid Wi-Fi country syntax")

    previous = read_state(
        cmdline_path,
        raspi_config=raspi_config,
        iw_command=iw_command,
        run_command=run_command,
    )
    previous_boot = ",".join(read_boot_countries(cmdline_path)) or "unconfigured"
    log(f"Requested Wi-Fi country: {desired}")
    log(f"Previous configured country: {previous.configured or 'unconfigured'}")
    log(f"Previous boot regdom: {previous_boot}")
    log(f"Previous runtime country: {previous.runtime or 'unconfigured'}")
    if previous.consistent and previous.configured == desired:
        log(f"Wi-Fi regulatory country already verified: {desired}")
        return previous
    log(f"Applying Wi-Fi regulatory country: {desired}")

    configured = _invoke(
        [raspi_config, "nonint", "do_wifi_country", desired],
        run_command=run_command,
        timeout=30,
        environment=_raspi_config_environment(),
    )
    if configured.returncode != 0:
        # Recent Raspberry Pi OS versions may update the regulatory state and
        # then return non-zero while trying to notify a desktop panel over a
        # session D-Bus.  The daemon deliberately has no desktop session.  Do
        # not confuse that optional notification failure with persistence:
        # reconcile the authoritative boot value below and decide success only
        # from the complete configured/boot/runtime postcondition.
        log(
            "raspi-config returned non-zero; continuing with explicit "
            "persistence and postcondition verification"
        )

    try:
        reconcile_boot_country(cmdline_path, desired)
    except OSError as exc:
        raise RegulatoryError(
            "boot regulatory configuration could not be persisted"
        ) from exc

    runtime_set = _invoke(
        [iw_command, "reg", "set", desired], run_command=run_command
    )
    if runtime_set.returncode != 0:
        raise RegulatoryError(
            "persistent Wi-Fi country was saved but runtime apply failed; reboot required"
        )

    state = RegulatoryState(None, None, None, False)
    for attempt in range(5):
        state = read_state(
            cmdline_path,
            raspi_config=raspi_config,
            iw_command=iw_command,
            run_command=run_command,
        )
        if state.consistent and state.configured == desired:
            break
        if attempt < 4:
            sleep(0.2)

    if state.configured != desired:
        raise RegulatoryError("persistent Wi-Fi country verification failed")
    if state.boot != desired:
        raise RegulatoryError("boot regulatory configuration verification failed")
    if state.runtime != desired:
        raise RegulatoryError(
            "runtime regulatory country verification failed; reboot required"
        )

    log(f"Persistent Wi-Fi country verified: {state.configured}")
    log(f"Boot regulatory configuration verified: {state.boot}")
    log(f"Runtime regulatory country verified: {state.runtime}")
    return state


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--country", required=True)
    parser.add_argument("--cmdline-path", default=os.getenv("PINS_WIFI_CMDLINE_PATH"))
    parser.add_argument("--iso3166-path", default=str(DEFAULT_ISO3166_PATH))
    parser.add_argument("--raspi-config", default=DEFAULT_RASPI_CONFIG)
    parser.add_argument("--iw", default=DEFAULT_IW)
    args = parser.parse_args(argv)

    try:
        desired = validate_supported_country(args.country, Path(args.iso3166_path))
        state = apply_country(
            desired,
            find_cmdline_path(args.cmdline_path),
            raspi_config=args.raspi_config,
            iw_command=args.iw,
            log=print,
        )
    except RegulatoryError as exc:
        print(f"Localization error: {exc}", file=os.sys.stderr)
        return 2
    print(
        "Wi-Fi regulatory update completed successfully: "
        f"configured={state.configured} boot={state.boot} runtime={state.runtime}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
