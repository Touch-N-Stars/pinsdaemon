#!/usr/bin/env python3
"""Provision and activate PINS client Wi-Fi profiles without persisting secrets.

The password is accepted only on stdin. NetworkManager is the sole durable
credential store; the PINS JSON configuration contains profile identity only.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


CONFIG_DEFAULT = "/opt/pinsdaemon/app/wifi_config.json"
VIABILITY_COMMAND = os.environ.get(
    "PINS_WIFI_VIABILITY_COMMAND",
    os.path.join(os.path.dirname(__file__), "pins-wifi-viability.py"),
)
PEER_STATE_FILE = os.environ.get(
    "PINS_WIFI_PEER_STATE_FILE", "/run/pins-wifi-watchdog.peer.json"
)
PROFILE_NAMESPACE = uuid.UUID("4db788e1-dd38-4a11-98ba-342264f0506c")
IFACE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
VALID_BANDS = {"", "a", "bg"}
try:
    CLIENT_AUTOCONNECT_PRIORITY = int(os.environ.get("PINS_CLIENT_AUTOCONNECT_PRIORITY", "100"))
except ValueError:
    CLIENT_AUTOCONNECT_PRIORITY = 100
ERROR_CODES = {
    "MISSING_CREDENTIALS",
    "INVALID_CREDENTIALS",
    "NETWORK_NOT_FOUND",
    "PROFILE_NOT_FOUND",
    "ASSOCIATION_FAILED",
    "IP_CONFIGURATION_FAILED",
    "GATEWAY_UNREACHABLE",
    "INTERFACE_UNAVAILABLE",
    "HOTSPOT_SWITCH_FAILED",
    "UNKNOWN",
}


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class WifiFailure(Exception):
    def __init__(self, code: str, message: str):
        if code not in ERROR_CODES:
            code = "UNKNOWN"
        super().__init__(message)
        self.code = code
        self.safe_message = message


class Nmcli:
    def run(self, args: Iterable[str], *, timeout: int = 60) -> CommandResult:
        try:
            proc = subprocess.run(
                ["nmcli", *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(124, "", type(exc).__name__)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def profile_identity(ssid: str) -> tuple[str, str]:
    digest = hashlib.sha256(ssid.encode("utf-8")).hexdigest()[:16]
    return f"pins-client-{digest}", str(uuid.uuid5(PROFILE_NAMESPACE, ssid))


def _split_terse(line: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    return fields


def _profile_exists(nm: Nmcli, profile_uuid: str) -> bool:
    return nm.run(["connection", "show", "uuid", profile_uuid]).returncode == 0


def _profile_value(nm: Nmcli, profile_uuid: str, field: str, *, secrets: bool = False) -> str:
    args = ["--show-secrets"] if secrets else []
    result = nm.run([*args, "-g", field, "connection", "show", "uuid", profile_uuid])
    return result.stdout.strip() if result.returncode == 0 else ""


def _profile_has_usable_credentials(nm: Nmcli, profile_uuid: str) -> bool:
    key_mgmt = _profile_value(nm, profile_uuid, "802-11-wireless-security.key-mgmt")
    if not key_mgmt:
        return True
    return bool(_profile_value(nm, profile_uuid, "802-11-wireless-security.psk", secrets=True))


def _wifi_profile_uuids(nm: Nmcli) -> list[str]:
    result = nm.run(["-t", "-f", "UUID,TYPE", "connection", "show"])
    if result.returncode != 0:
        return []
    uuids = []
    for line in result.stdout.splitlines():
        fields = _split_terse(line)
        if len(fields) >= 2 and fields[1] == "802-11-wireless":
            uuids.append(fields[0])
    return uuids


def _find_saved_profile(nm: Nmcli, ssid: str, configured_uuid: str | None) -> str:
    candidates: list[str] = []
    if configured_uuid and _profile_exists(nm, configured_uuid):
        candidates.append(configured_uuid)
    _, canonical_uuid = profile_identity(ssid)
    if canonical_uuid not in candidates and _profile_exists(nm, canonical_uuid):
        candidates.append(canonical_uuid)
    for profile_uuid in _wifi_profile_uuids(nm):
        if profile_uuid not in candidates:
            candidates.append(profile_uuid)

    matching = [
        candidate
        for candidate in candidates
        if _profile_value(nm, candidate, "802-11-wireless.ssid") == ssid
        and _profile_value(nm, candidate, "802-11-wireless.mode") != "ap"
    ]
    if not matching:
        raise WifiFailure("PROFILE_NOT_FOUND", "No_saved_profile_for_requested_network")
    usable = [candidate for candidate in matching if _profile_has_usable_credentials(nm, candidate)]
    if not usable:
        for candidate in matching:
            nm.run(["connection", "modify", "uuid", candidate, "connection.autoconnect", "no"])
        raise WifiFailure("MISSING_CREDENTIALS", "Saved_profile_has_no_NetworkManager_secret")
    if canonical_uuid in usable:
        return canonical_uuid
    if configured_uuid in usable:
        return configured_uuid
    if len(usable) == 1:
        return usable[0]
    raise WifiFailure("PROFILE_NOT_FOUND", "Multiple_saved_profiles_require_reconfiguration")


def _scan_security(nm: Nmcli, interface: str, ssid: str) -> str:
    nm.run(["device", "wifi", "rescan", "ifname", interface], timeout=20)
    time.sleep(2)
    result = nm.run(["-t", "--escape", "yes", "-f", "SSID,SECURITY", "device", "wifi", "list", "ifname", interface])
    if result.returncode != 0:
        raise WifiFailure("INTERFACE_UNAVAILABLE", "Wi-Fi_scan_failed")
    for line in result.stdout.splitlines():
        fields = _split_terse(line)
        if fields and fields[0] == ssid:
            return fields[1] if len(fields) > 1 else ""
    raise WifiFailure("NETWORK_NOT_FOUND", "Requested_network_not_visible")


def _classify_activation(result: CommandResult, password_supplied: bool) -> WifiFailure:
    text = f"{result.stdout}\n{result.stderr}".lower()
    if "secrets were required" in text or "passwords or encryption keys are required" in text:
        if password_supplied:
            return WifiFailure("INVALID_CREDENTIALS", "Wi-Fi_authentication_failed")
        return WifiFailure("MISSING_CREDENTIALS", "NetworkManager_requires_a_saved_secret")
    if "no network with ssid" in text or "not found" in text:
        return WifiFailure("NETWORK_NOT_FOUND", "Requested_network_not_visible")
    if "ip configuration" in text or "ip-config" in text:
        return WifiFailure("IP_CONFIGURATION_FAILED", "Client_IP_configuration_failed")
    if password_supplied and ("secret" in text or "authentication" in text or "802.1x" in text):
        return WifiFailure("INVALID_CREDENTIALS", "Wi-Fi_authentication_failed")
    return WifiFailure("ASSOCIATION_FAILED", "Wi-Fi_profile_activation_failed")


def _activate(nm: Nmcli, profile_uuid: str, interface: str, password_supplied: bool) -> None:
    result = nm.run(["--wait", "60", "connection", "up", "uuid", profile_uuid, "ifname", interface], timeout=70)
    if result.returncode != 0:
        raise _classify_activation(result, password_supplied)
    active = nm.run(["-g", "GENERAL.CON-UUID,IP4.ADDRESS", "device", "show", interface])
    lines = [line.strip() for line in active.stdout.splitlines() if line.strip()]
    if active.returncode != 0 or not lines or lines[0] != profile_uuid:
        raise WifiFailure("ASSOCIATION_FAILED", "Activated_profile_is_not_active_on_interface")
    if not any("/" in line for line in lines[1:]):
        raise WifiFailure("IP_CONFIGURATION_FAILED", "Client_has_no_IPv4_address")


def _run_viability_check(interface: str) -> tuple[subprocess.CompletedProcess[str], str]:
    result = subprocess.run(
        [
            VIABILITY_COMMAND,
            "--interface",
            interface,
            "--peer-state-file",
            PEER_STATE_FILE,
            "--connection-commit",
            "--confirm-peer",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    safe_output = ""
    for line in result.stdout.strip().splitlines():
        if line.startswith("PINS_WIFI_VIABILITY "):
            safe_output = line
    if safe_output:
        print(safe_output)
    return result, safe_output


def _viability_succeeded(
    result: subprocess.CompletedProcess[str], safe_output: str
) -> bool:
    return result.returncode == 0 and safe_output.startswith(
        (
            "PINS_WIFI_VIABILITY status=healthy mode=gateway reason=",
            "PINS_WIFI_VIABILITY status=healthy mode=pins-peer reason=",
        )
    )


def _deactivate_parallel_pins_hotspot(
    nm: Nmcli,
    client_interface: str,
    hotspot_interface: str,
    conflicting_address: str,
) -> bool:
    """Suspend the managed local AP if it overlaps a remote PINS hotspot."""
    if client_interface == hotspot_interface:
        return False
    try:
        expected_address = ipaddress.IPv4Address(conflicting_address)
    except ipaddress.AddressValueError:
        return False
    addresses = nm.run(["-g", "IP4.ADDRESS", "device", "show", hotspot_interface])
    if addresses.returncode != 0:
        return False
    owns_conflicting_address = False
    for value in addresses.stdout.splitlines():
        try:
            owns_conflicting_address = (
                ipaddress.IPv4Interface(value.strip()).ip == expected_address
            )
        except ValueError:
            continue
        if owns_conflicting_address:
            break
    if not owns_conflicting_address:
        return False
    active = nm.run(
        [
            "-t",
            "--escape",
            "yes",
            "-f",
            "UUID,TYPE,DEVICE",
            "connection",
            "show",
            "--active",
        ]
    )
    if active.returncode != 0:
        return False
    for line in active.stdout.splitlines():
        fields = _split_terse(line)
        if len(fields) != 3 or fields[2] != hotspot_interface:
            continue
        if fields[1] not in {"802-11-wireless", "wifi"}:
            continue
        profile_uuid = fields[0]
        if _profile_value(nm, profile_uuid, "802-11-wireless.mode") != "ap":
            continue
        profile_id = _profile_value(nm, profile_uuid, "connection.id")
        if profile_id != "Hotspot" and not profile_id.startswith("pins-hotspot-"):
            continue
        if nm.run(
            [
                "connection",
                "modify",
                "uuid",
                profile_uuid,
                "connection.autoconnect",
                "no",
            ]
        ).returncode != 0:
            return False
        if nm.run(["connection", "down", "uuid", profile_uuid], timeout=30).returncode == 0:
            return True
        nm.run(
            [
                "connection",
                "modify",
                "uuid",
                profile_uuid,
                "connection.autoconnect",
                "yes",
            ]
        )
        return False
    return False


def _verify_connection_commit(
    nm: Nmcli, interface: str, hotspot_interface: str
) -> None:
    """Require a verified PINS peer only when DHCP supplies no gateway."""
    try:
        result, safe_output = _run_viability_check(interface)
    except (OSError, subprocess.TimeoutExpired):
        raise WifiFailure("UNKNOWN", "Wi-Fi_viability_checker_is_unavailable")

    local_address_match = re.search(
        r"(?:^| )reason=dhcp_server_is_local_address:([0-9.]+)(?: |$)",
        safe_output,
    )
    if (
        result.returncode != 0
        and " mode=pins-peer " in f" {safe_output} "
        and local_address_match is not None
        and _deactivate_parallel_pins_hotspot(
            nm, interface, hotspot_interface, local_address_match.group(1)
        )
    ):
        try:
            result, safe_output = _run_viability_check(interface)
        except (OSError, subprocess.TimeoutExpired):
            raise WifiFailure("UNKNOWN", "Wi-Fi_viability_checker_is_unavailable")

    if result.returncode == 0 and not _viability_succeeded(result, safe_output):
        raise WifiFailure("UNKNOWN", "Wi-Fi_viability_checker_returned_invalid_output")
    if result.returncode != 0:
        raise WifiFailure(
            "GATEWAY_UNREACHABLE",
            "Client_without_gateway_requires_a_verified_PINS_peer",
        )


def _delete_profile(nm: Nmcli, profile_uuid: str) -> None:
    if _profile_exists(nm, profile_uuid):
        nm.run(["connection", "delete", "uuid", profile_uuid])


def _create_profile(
    nm: Nmcli,
    *,
    profile_id: str,
    profile_uuid: str,
    ssid: str,
    interface: str,
    password: str,
    secured: bool,
    band: str,
    auto_connect: bool,
) -> None:
    args = [
        "connection", "add", "type", "wifi", "ifname", interface,
        "con-name", profile_id, "connection.uuid", profile_uuid,
        "ssid", ssid,
    ]
    result = nm.run(args)
    if result.returncode != 0:
        raise WifiFailure("UNKNOWN", "Failed_to_create_NetworkManager_profile")
    settings = [
        "connection", "modify", "uuid", profile_uuid,
        "connection.interface-name", interface,
        "connection.autoconnect", "yes" if auto_connect else "no",
        "connection.autoconnect-priority", str(CLIENT_AUTOCONNECT_PRIORITY),
        "802-11-wireless.powersave", "2",
        "ipv4.method", "auto",
    ]
    if band:
        settings.extend(["802-11-wireless.band", band])
    if secured:
        settings.extend([
            "802-11-wireless-security.key-mgmt", "wpa-psk",
            "802-11-wireless-security.psk", password,
            "802-11-wireless-security.psk-flags", "0",
        ])
    result = nm.run(settings)
    if result.returncode != 0:
        _delete_profile(nm, profile_uuid)
        raise WifiFailure("UNKNOWN", "Failed_to_configure_NetworkManager_profile")


def _load_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _persist_success(path: Path, *, ssid: str, auto_connect: bool, band: str, client: str, hotspot: str, profile_uuid: str) -> None:
    current = _load_config(path)
    if auto_connect:
        current.update({"ssid": ssid, "auto_connect": True, "band": {"a": "5GHz", "bg": "2.4GHz"}.get(band), "client_profile_uuid": profile_uuid})
    current.update({"client_interface": client, "hotspot_interface": hotspot, "desired_mode": "auto"})
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.stat() if path.exists() else None
    fd, temp_name = tempfile.mkstemp(prefix=".wifi_config.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(current, handle, indent=4)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if previous and hasattr(os, "chown"):
            os.chown(temp_name, previous.st_uid, previous.st_gid)
            os.chmod(temp_name, previous.st_mode & 0o777)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def connect(args: argparse.Namespace, password: str, nm: Nmcli) -> str:
    config_path = Path(args.config_file)
    config = _load_config(config_path)
    configured_uuid = config.get("client_profile_uuid") if isinstance(config.get("client_profile_uuid"), str) else None
    if not IFACE_RE.fullmatch(args.client_iface) or not IFACE_RE.fullmatch(args.hotspot_iface):
        raise WifiFailure("INTERFACE_UNAVAILABLE", "Invalid_Wi-Fi_interface")
    if args.band not in VALID_BANDS:
        raise WifiFailure("UNKNOWN", "Unsupported_Wi-Fi_band")
    if nm.run(["device", "show", args.client_iface]).returncode != 0:
        raise WifiFailure("INTERFACE_UNAVAILABLE", "Client_Wi-Fi_interface_is_unavailable")

    if not password:
        profile_uuid = _find_saved_profile(nm, args.ssid, configured_uuid)
        _activate(nm, profile_uuid, args.client_iface, False)
        _verify_connection_commit(nm, args.client_iface, args.hotspot_iface)
    else:
        security = _scan_security(nm, args.client_iface, args.ssid)
        secured = bool(security and security != "--")
        if secured and len(password) < 8:
            raise WifiFailure("INVALID_CREDENTIALS", "Wi-Fi_password_is_too_short")
        canonical_id, canonical_uuid = profile_identity(args.ssid)
        candidate_uuid = str(uuid.uuid4())
        candidate_id = f"pins-candidate-{candidate_uuid[:8]}"
        _create_profile(nm, profile_id=candidate_id, profile_uuid=candidate_uuid, ssid=args.ssid, interface=args.client_iface, password=password, secured=secured, band=args.band, auto_connect=False)
        try:
            _activate(nm, candidate_uuid, args.client_iface, True)
            _verify_connection_commit(nm, args.client_iface, args.hotspot_iface)
            _delete_profile(nm, canonical_uuid)
            _create_profile(nm, profile_id=canonical_id, profile_uuid=canonical_uuid, ssid=args.ssid, interface=args.client_iface, password=password, secured=secured, band=args.band, auto_connect=args.auto_connect)
            _activate(nm, canonical_uuid, args.client_iface, True)
            _verify_connection_commit(nm, args.client_iface, args.hotspot_iface)
            profile_uuid = canonical_uuid
        except Exception:
            _delete_profile(nm, candidate_uuid)
            raise
        _delete_profile(nm, candidate_uuid)
        for other_uuid in _wifi_profile_uuids(nm):
            if (
                other_uuid != profile_uuid
                and _profile_value(nm, other_uuid, "802-11-wireless.ssid") == args.ssid
                and _profile_value(nm, other_uuid, "802-11-wireless.mode") != "ap"
            ):
                nm.run(["connection", "modify", "uuid", other_uuid, "connection.autoconnect", "no"])

    if args.band:
        result = nm.run(["connection", "modify", "uuid", profile_uuid, "802-11-wireless.band", args.band])
        if result.returncode != 0:
            raise WifiFailure("ASSOCIATION_FAILED", "Failed_to_apply_band_preference")
    result = nm.run([
        "connection", "modify", "uuid", profile_uuid,
        "connection.autoconnect", "yes" if args.auto_connect else "no",
    ])
    if result.returncode != 0:
        raise WifiFailure("UNKNOWN", "Failed_to_apply_autoconnect_policy")
    nm.run(["connection", "modify", "uuid", profile_uuid, "connection.autoconnect-priority", "100"])
    _persist_success(config_path, ssid=args.ssid, auto_connect=args.auto_connect, band=args.band, client=args.client_iface, hotspot=args.hotspot_iface, profile_uuid=profile_uuid)
    return profile_uuid


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-iface", required=True)
    parser.add_argument("--hotspot-iface", required=True)
    parser.add_argument("--ssid", required=True)
    parser.add_argument("--band", default="")
    parser.add_argument("--auto-connect", choices=("yes", "no"), default="no")
    parser.add_argument("--config-file", default=os.environ.get("WIFI_CONFIG_FILE", CONFIG_DEFAULT))
    parsed = parser.parse_args()
    parsed.auto_connect = parsed.auto_connect == "yes"
    return parsed


def main() -> int:
    args = parse_args()
    password = sys.stdin.readline().rstrip("\r\n")
    try:
        connect(args, password, Nmcli())
    except WifiFailure as exc:
        print(f"PINS_WIFI_RESULT code={exc.code} message={exc.safe_message}")
        return 1
    except Exception:
        print("PINS_WIFI_RESULT code=UNKNOWN message=Unexpected_Wi-Fi_management_failure")
        return 1
    finally:
        password = ""
    print("PINS_WIFI_RESULT code=SUCCESS message=Client_connection_verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
