#!/usr/bin/env python3
"""Validate a PINS Wi-Fi client connection without weakening AP fallback.

Normal networks keep the historic default-gateway ping check. A client
network without a default gateway is accepted only when NetworkManager shows
an active infrastructure profile with IPv4 configuration and the DHCP server
answers with a stable, valid pinsdaemon identity on the unauthenticated
``/health`` endpoint.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_PEER_STATE_FILE = "/run/pins-wifi-watchdog.peer.json"
DEFAULT_LOCAL_RIG_NAME_FILE = "/etc/pins/rig-name"
RIG_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
INTERFACE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_HEALTH_RESPONSE_BYTES = 64 * 1024
PEER_CONFIRMATION_MAX_AGE_SECONDS = 30


@dataclass(frozen=True)
class ValidationResult:
    status: str
    mode: str
    reason: str
    rig_id: str = ""

    @property
    def healthy(self) -> bool:
        return self.status == "healthy"

    @property
    def pending(self) -> bool:
        return self.status == "pending"

    def log_line(self) -> str:
        fields = [
            "PINS_WIFI_VIABILITY",
            f"status={self.status}",
            f"mode={self.mode}",
            f"reason={self.reason}",
        ]
        if self.rig_id:
            fields.append(f"rig_id={self.rig_id}")
        return " ".join(fields)


@dataclass(frozen=True)
class ClientSnapshot:
    profile_uuid: str
    activation_timestamp: str
    addresses: tuple[ipaddress.IPv4Interface, ...]


class ProbeFailure(RuntimeError):
    """A required local networking probe could not be completed."""


def _split_terse(line: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    return fields


def _valid_client_address(value: str) -> ipaddress.IPv4Interface | None:
    try:
        address = ipaddress.IPv4Interface(value)
    except ValueError:
        return None
    ip = address.ip
    if ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return None
    return address


def _parse_dhcp_server(options: Iterable[str]) -> ipaddress.IPv4Address | None:
    pattern = re.compile(
        r"(?:^|\s)(?:dhcp_)?server_identifier\s*(?:=|:)\s*"
        r"([0-9]{1,3}(?:\.[0-9]{1,3}){3})(?:\s|$)",
        re.IGNORECASE,
    )
    for option in options:
        match = pattern.search(option.strip())
        if not match:
            continue
        try:
            server = ipaddress.IPv4Address(match.group(1))
        except ValueError:
            continue
        if not (
            server.is_unspecified
            or server.is_loopback
            or server.is_link_local
            or server.is_multicast
        ):
            return server
    return None


def _valid_health_identity(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    rig_id = payload.get("rigId")
    api_version = payload.get("apiVersion")
    if payload.get("status") != "ok" or payload.get("service") != "pinsdaemon":
        return None
    if not isinstance(rig_id, str) or not RIG_ID_RE.fullmatch(rig_id):
        return None
    if isinstance(api_version, bool) or not isinstance(api_version, int) or api_version < 2:
        return None
    return rig_id


class SystemProbe:
    def _run(self, command: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["LC_ALL"] = "C"
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            return subprocess.CompletedProcess(command, 124, "", "command_failed")

    def _nmcli_values(self, field: str, *args: str) -> list[str]:
        result = self._run(["nmcli", "-g", field, *args])
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def client_snapshot(self, interface: str) -> ClientSnapshot | None:
        active = self._run(
            [
                "nmcli",
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
            return None

        active_uuid = ""
        for line in active.stdout.splitlines():
            fields = _split_terse(line)
            if len(fields) != 3:
                continue
            if fields[1] in {"802-11-wireless", "wifi"} and fields[2] == interface:
                active_uuid = fields[0]
                break
        if not active_uuid:
            return None

        mode_result = self._run(
            [
                "nmcli",
                "-g",
                "802-11-wireless.mode",
                "connection",
                "show",
                "uuid",
                active_uuid,
            ]
        )
        if mode_result.returncode != 0:
            return None
        mode = [
            line.strip()
            for line in mode_result.stdout.splitlines()
            if line.strip()
        ]
        # NetworkManager documents a blank wireless mode as infrastructure.
        # Accept that default representation as well as the explicit value,
        # but never accept AP, mesh, or ad-hoc profiles here.
        if mode not in ([], ["infrastructure"]):
            return None

        state = self._nmcli_values("GENERAL.STATE", "device", "show", interface)
        if not state or not re.match(r"^100(?:\s|$)", state[0]):
            return None
        device_uuid = self._nmcli_values("GENERAL.CON-UUID", "device", "show", interface)
        if device_uuid != [active_uuid]:
            return None

        addresses = tuple(
            address
            for value in self._nmcli_values("IP4.ADDRESS", "device", "show", interface)
            if (address := _valid_client_address(value)) is not None
        )
        if not addresses:
            return None

        timestamp = self._nmcli_values(
            "connection.timestamp", "connection", "show", "uuid", active_uuid
        )
        if len(timestamp) != 1 or not re.fullmatch(r"[1-9][0-9]*", timestamp[0]):
            return None
        return ClientSnapshot(
            profile_uuid=active_uuid,
            activation_timestamp=timestamp[0],
            addresses=addresses,
        )

    def default_gateway(self, interface: str) -> ipaddress.IPv4Address | None:
        result = self._run(["ip", "-4", "route", "show", "dev", interface, "default"])
        if result.returncode != 0:
            raise ProbeFailure("default_route_probe_failed")
        for line in result.stdout.splitlines():
            fields = line.split()
            if not fields or fields[0] != "default" or "via" not in fields:
                continue
            try:
                return ipaddress.IPv4Address(fields[fields.index("via") + 1])
            except (ValueError, IndexError):
                continue
        return None

    def ping_gateway(self, interface: str, gateway: ipaddress.IPv4Address, timeout: int) -> bool:
        return (
            self._run(
                [
                    "ping",
                    "-I",
                    interface,
                    "-c",
                    "1",
                    "-W",
                    str(timeout),
                    str(gateway),
                ],
                timeout=timeout + 2,
            ).returncode
            == 0
        )

    def dhcp_server(self, interface: str) -> ipaddress.IPv4Address | None:
        return _parse_dhcp_server(
            self._nmcli_values("DHCP4.OPTION", "device", "show", interface)
        )

    def local_ipv4_addresses(self) -> set[ipaddress.IPv4Address]:
        result = self._run(["ip", "-4", "-o", "address", "show"])
        if result.returncode != 0:
            raise ProbeFailure("local_address_probe_failed")
        addresses: set[ipaddress.IPv4Address] = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            for index, field in enumerate(fields[:-1]):
                if field != "inet":
                    continue
                try:
                    addresses.add(ipaddress.IPv4Interface(fields[index + 1]).ip)
                except ValueError:
                    pass
        return addresses

    def peer_health(
        self, server: ipaddress.IPv4Address, interface: str, timeout: float
    ) -> str | None:
        class BoundHTTPConnection(http.client.HTTPConnection):
            def connect(bound_self) -> None:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.settimeout(timeout)
                    if hasattr(socket, "SO_BINDTODEVICE"):
                        sock.setsockopt(
                            socket.SOL_SOCKET,
                            socket.SO_BINDTODEVICE,
                            interface.encode("ascii") + b"\0",
                        )
                    sock.connect((str(server), 8000))
                    bound_self.sock = sock
                except Exception:
                    sock.close()
                    raise

        connection = BoundHTTPConnection(str(server), 8000, timeout=timeout)
        try:
            connection.request(
                "GET",
                "/health",
                headers={"Accept": "application/json", "Connection": "close"},
            )
            response = connection.getresponse()
            body = response.read(MAX_HEALTH_RESPONSE_BYTES + 1)
            if response.status != 200 or len(body) > MAX_HEALTH_RESPONSE_BYTES:
                return None
            content_type = response.getheader("Content-Type", "").lower()
            if "application/json" not in content_type:
                return None
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return None
            return _valid_health_identity(payload)
        except (OSError, http.client.HTTPException):
            return None
        finally:
            connection.close()


def _read_local_rig_id(path: Path) -> str:
    configured = os.environ.get("PINS_RIG_ID", "").strip()
    if configured:
        candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", configured).strip("-").lower()
        if candidate and RIG_ID_RE.fullmatch(candidate):
            return candidate
    try:
        raw = path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return ""
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-").lower()[:63]
    return candidate if RIG_ID_RE.fullmatch(candidate) else ""


def _read_peer_state(path: Path) -> dict:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_peer_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _clear_peer_state(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
    except OSError:
        pass


def validate_once(
    probe: SystemProbe,
    *,
    interface: str,
    peer_state_file: Path,
    local_rig_name_file: Path,
    ping_timeout: int,
    health_timeout: float,
    probe_gateway: bool,
) -> ValidationResult:
    try:
        gateway = probe.default_gateway(interface)
    except ProbeFailure:
        _clear_peer_state(peer_state_file)
        return ValidationResult("unhealthy", "none", "default_route_probe_failed")
    if gateway is not None:
        _clear_peer_state(peer_state_file)
        if not probe_gateway:
            return ValidationResult("healthy", "gateway", "gateway_present")
        if probe.ping_gateway(interface, gateway, ping_timeout):
            return ValidationResult("healthy", "gateway", "gateway_reachable")
        return ValidationResult("unhealthy", "gateway", "gateway_unreachable")

    snapshot = probe.client_snapshot(interface)
    if snapshot is None:
        _clear_peer_state(peer_state_file)
        return ValidationResult("unhealthy", "none", "active_infrastructure_client_required")

    dhcp_server = probe.dhcp_server(interface)
    if dhcp_server is None:
        _clear_peer_state(peer_state_file)
        return ValidationResult("unhealthy", "pins-peer", "dhcp_server_unknown")
    if not any(dhcp_server in address.network for address in snapshot.addresses):
        _clear_peer_state(peer_state_file)
        return ValidationResult("unhealthy", "pins-peer", "dhcp_server_not_on_link")
    if any(dhcp_server == address.ip for address in snapshot.addresses):
        _clear_peer_state(peer_state_file)
        return ValidationResult(
            "unhealthy",
            "pins-peer",
            f"dhcp_server_is_local_address:{dhcp_server}",
        )
    try:
        local_addresses = probe.local_ipv4_addresses()
    except ProbeFailure:
        _clear_peer_state(peer_state_file)
        return ValidationResult("unhealthy", "pins-peer", "local_address_probe_failed")
    if dhcp_server in local_addresses:
        _clear_peer_state(peer_state_file)
        return ValidationResult(
            "unhealthy",
            "pins-peer",
            f"dhcp_server_is_local_address:{dhcp_server}",
        )

    rig_id = probe.peer_health(dhcp_server, interface, health_timeout)
    local_rig_id = _read_local_rig_id(local_rig_name_file)
    if not rig_id:
        _clear_peer_state(peer_state_file)
        return ValidationResult("unhealthy", "pins-peer", "pins_health_unavailable")
    if local_rig_id and rig_id == local_rig_id:
        _clear_peer_state(peer_state_file)
        return ValidationResult("unhealthy", "pins-peer", "peer_identity_matches_local_rig")

    identity = {
        "schema": 1,
        "interface": interface,
        "profile_uuid": snapshot.profile_uuid,
        "activation_timestamp": snapshot.activation_timestamp,
        "dhcp_server": str(dhcp_server),
        "rig_id": rig_id,
    }
    previous = _read_peer_state(peer_state_file)
    previous_observed_at = previous.get("observed_at")
    now = int(time.time())
    recent = (
        isinstance(previous_observed_at, int)
        and not isinstance(previous_observed_at, bool)
        and 0 <= now - previous_observed_at <= PEER_CONFIRMATION_MAX_AGE_SECONDS
    )
    stable = recent and all(previous.get(key) == value for key, value in identity.items())
    identity["confirmed"] = stable
    identity["observed_at"] = now
    _write_peer_state(peer_state_file, identity)
    if not stable:
        return ValidationResult("pending", "pins-peer", "peer_identity_pending", rig_id)
    return ValidationResult("healthy", "pins-peer", "peer_identity_confirmed", rig_id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", required=True)
    parser.add_argument(
        "--peer-state-file",
        default=os.environ.get("PINS_WIFI_PEER_STATE_FILE", DEFAULT_PEER_STATE_FILE),
    )
    parser.add_argument(
        "--local-rig-name-file",
        default=os.environ.get("PINS_RIG_NAME_FILE", DEFAULT_LOCAL_RIG_NAME_FILE),
    )
    parser.add_argument("--ping-timeout", type=int, default=2)
    parser.add_argument("--health-timeout", type=float, default=2.0)
    parser.add_argument("--confirm-peer", action="store_true")
    parser.add_argument("--confirmation-delay", type=float, default=1.0)
    parser.add_argument(
        "--connection-commit",
        action="store_true",
        help="Keep historic initial handling for networks that advertise a gateway.",
    )
    args = parser.parse_args()
    if not INTERFACE_RE.fullmatch(args.interface):
        parser.error("invalid interface")
    if not 1 <= args.ping_timeout <= 10:
        parser.error("ping timeout must be between 1 and 10 seconds")
    if not 0.1 <= args.health_timeout <= 10:
        parser.error("health timeout must be between 0.1 and 10 seconds")
    if not 0 <= args.confirmation_delay <= 5:
        parser.error("confirmation delay must be between 0 and 5 seconds")
    return args


def main() -> int:
    args = parse_args()
    kwargs = {
        "interface": args.interface,
        "peer_state_file": Path(args.peer_state_file),
        "local_rig_name_file": Path(args.local_rig_name_file),
        "ping_timeout": args.ping_timeout,
        "health_timeout": args.health_timeout,
        "probe_gateway": not args.connection_commit,
    }
    probe = SystemProbe()
    result = validate_once(probe, **kwargs)
    if args.confirm_peer and result.pending:
        time.sleep(args.confirmation_delay)
        result = validate_once(probe, **kwargs)
    print(result.log_line())
    if result.healthy:
        return 0
    return 2 if result.pending else 1


if __name__ == "__main__":
    raise SystemExit(main())
