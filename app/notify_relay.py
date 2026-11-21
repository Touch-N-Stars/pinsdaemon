"""Session notification relay.

Collects the notifications a phone should see while Touch-N-Stars is in the
background or closed, so the app (or its native background listener) can pick
them up over the local network and raise them as phone notifications:

* PINS/N.I.N.A. notifications, read from the PINS SignalR hub on
  ``ws://127.0.0.1:4782/hubs/notifications``. PINS broadcasts them
  fire-and-forget to whoever is connected at that moment, so a phone that was
  asleep loses them; the relay is always connected and keeps them.
* Host events the daemon can observe itself: Raspberry Pi under-voltage,
  CPU temperature over a threshold and low free disk space.

Every event gets a sequence number and is kept in a bounded in-memory buffer.
Clients ask for everything after the last sequence number they saw. ``bootId``
changes whenever the daemon restarts, which tells clients that sequence
numbers restarted too.

Optionally, events at or above a severity are forwarded to an ntfy topic when
the Pi has internet access. That is the only path that reaches a phone outside
the rig's Wi-Fi.

The relay never mutates the host. It only reads ``vcgencmd``/sysfs through the
probe callables it is given and opens outbound connections.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Set

SEVERITIES = ("info", "success", "warning", "error")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}
# PINS serialises NINA.Core.Utility.Notification.NotificationType as an int.
NINA_TYPE_TO_SEVERITY = {0: "info", 1: "success", 2: "warning", 3: "error"}

# Noise PINS emits on every start with a non-10Micron mount; Touch-N-Stars
# already hides it from its toasts.
IGNORED_NINA_MESSAGES = {"Not a 10u mount. 10u utilities disabled."}

CONFIG_FILE = os.getenv(
    "NOTIFY_CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "notify_config.json"),
)
PINS_NOTIFICATION_HUB_URL = os.getenv(
    "PINS_NOTIFICATION_HUB_URL", "ws://127.0.0.1:4782/hubs/notifications"
)
BUFFER_SIZE = 500
SYSTEM_CHECK_INTERVAL_SECONDS = 60.0
HUB_RECONNECT_MIN_SECONDS = 2.0
HUB_RECONNECT_MAX_SECONDS = 60.0
NTFY_TIMEOUT_SECONDS = 10.0
SIGNALR_RECORD_SEPARATOR = "\x1e"

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "includeNina": True,
    "includeSystem": True,
    "temperatureThresholdC": 80.0,
    "diskFreeThresholdGb": 2.0,
    "diskPath": "/",
    "ntfy": {
        "enabled": False,
        "url": "",
        "token": "",
        "minSeverity": "warning",
    },
}


def severity_at_least(severity: str, minimum: str) -> bool:
    return SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK.get(minimum, 0)


@dataclass
class RelayEvent:
    seq: int
    severity: str
    source: str  # "nina" | "system"
    title: str
    message: str
    timestamp: float
    code: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "severity": self.severity,
            "source": self.source,
            "title": self.title,
            "message": self.message,
            "timestamp": self.timestamp,
            "code": self.code,
        }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _coerce_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(minimum, min(maximum, number))


def normalize_config(data: Any) -> Dict[str, Any]:
    """Merge user input over the defaults, dropping anything invalid."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if not isinstance(data, dict):
        return config

    for key in ("enabled", "includeNina", "includeSystem"):
        if isinstance(data.get(key), bool):
            config[key] = data[key]
    config["temperatureThresholdC"] = _coerce_float(
        data.get("temperatureThresholdC"), config["temperatureThresholdC"], 40.0, 110.0
    )
    config["diskFreeThresholdGb"] = _coerce_float(
        data.get("diskFreeThresholdGb"), config["diskFreeThresholdGb"], 0.0, 10000.0
    )
    disk_path = data.get("diskPath")
    if isinstance(disk_path, str) and disk_path.startswith("/") and "\x00" not in disk_path:
        config["diskPath"] = disk_path

    ntfy = data.get("ntfy")
    if isinstance(ntfy, dict):
        if isinstance(ntfy.get("enabled"), bool):
            config["ntfy"]["enabled"] = ntfy["enabled"]
        url = ntfy.get("url")
        if isinstance(url, str):
            config["ntfy"]["url"] = url.strip() if _is_valid_ntfy_url(url.strip()) else ""
        token = ntfy.get("token")
        if isinstance(token, str) and "\n" not in token and "\r" not in token:
            config["ntfy"]["token"] = token.strip()
        if ntfy.get("minSeverity") in SEVERITY_RANK:
            config["ntfy"]["minSeverity"] = ntfy["minSeverity"]
    return config


def _is_valid_ntfy_url(url: str) -> bool:
    if not url:
        return True
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc) and len(parsed.path) > 1


def load_config(path: str = CONFIG_FILE) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return normalize_config(json.load(handle))
    except FileNotFoundError:
        return normalize_config(None)
    except Exception as exc:  # corrupt file: fall back to defaults, keep running
        print(f"[notify] could not read {path}: {exc}")
        return normalize_config(None)


def save_config(config: Dict[str, Any], path: str = CONFIG_FILE) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".notify_config.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=4)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # The file holds the ntfy access token.
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def public_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Config as returned by the API: the ntfy token is never sent back."""
    result = json.loads(json.dumps(config))
    result["ntfy"]["hasToken"] = bool(config["ntfy"].get("token"))
    result["ntfy"].pop("token", None)
    return result


def merge_config_update(current: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a partial update. An omitted ntfy token keeps the stored one."""
    merged = json.loads(json.dumps(current))
    for key, value in update.items():
        if key == "ntfy" and isinstance(value, dict):
            merged["ntfy"].update({k: v for k, v in value.items() if k != "hasToken"})
        else:
            merged[key] = value
    return normalize_config(merged)


# ---------------------------------------------------------------------------
# SignalR (JSON hub protocol over WebSockets, negotiation skipped)
# ---------------------------------------------------------------------------


def signalr_handshake() -> str:
    return json.dumps({"protocol": "json", "version": 1}) + SIGNALR_RECORD_SEPARATOR


def parse_signalr_frames(payload: str) -> List[Dict[str, Any]]:
    frames = []
    for chunk in payload.split(SIGNALR_RECORD_SEPARATOR):
        if not chunk.strip():
            continue
        try:
            frame = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(frame, dict):
            frames.append(frame)
    return frames


def nina_notification_fields(notification: Any) -> Optional[Dict[str, str]]:
    """Map a PINS ``NotificationMessage`` to severity/title/message."""
    if not isinstance(notification, dict):
        return None
    # ASP.NET Core serialises camelCase by default; accept PascalCase too.
    message = notification.get("message", notification.get("Message"))
    if not isinstance(message, str) or not message.strip():
        return None
    if message.strip() in IGNORED_NINA_MESSAGES:
        return None
    raw_type = notification.get("type", notification.get("Type", 0))
    if isinstance(raw_type, str):
        severity = {
            "information": "info",
            "success": "success",
            "warning": "warning",
            "error": "error",
        }.get(raw_type.lower(), "info")
    else:
        severity = NINA_TYPE_TO_SEVERITY.get(raw_type, "info")
    title = notification.get("title", notification.get("Title")) or ""
    return {"severity": severity, "title": str(title), "message": message.strip()}


# ---------------------------------------------------------------------------
# Relay
# ---------------------------------------------------------------------------

PowerProbe = Callable[[], Awaitable[Optional[Dict[str, Any]]]]
TemperatureProbe = Callable[[], Awaitable[Optional[float]]]


@dataclass
class _SystemState:
    under_voltage: bool = False
    over_temperature: bool = False
    low_disk: bool = False


@dataclass
class NotifyRelay:
    power_probe: Optional[PowerProbe] = None
    temperature_probe: Optional[TemperatureProbe] = None
    config_path: str = CONFIG_FILE
    hub_url: str = PINS_NOTIFICATION_HUB_URL
    buffer_size: int = BUFFER_SIZE
    boot_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    config: Dict[str, Any] = field(default_factory=dict)
    hub_connected: bool = False
    ntfy_last_error: Optional[str] = None

    def __post_init__(self) -> None:
        self._events: Deque[RelayEvent] = deque(maxlen=self.buffer_size)
        self._seq = 0
        self._listeners: Set[asyncio.Queue] = set()
        self._system = _SystemState()
        self._tasks: List[asyncio.Task] = []
        if not self.config:
            self.config = load_config(self.config_path)

    # -- buffer ----------------------------------------------------------

    @property
    def last_seq(self) -> int:
        return self._seq

    def publish(
        self,
        severity: str,
        source: str,
        message: str,
        title: str = "",
        code: Optional[str] = None,
    ) -> Optional[RelayEvent]:
        if not self.config.get("enabled", True):
            return None
        self._seq += 1
        event = RelayEvent(
            seq=self._seq,
            severity=severity if severity in SEVERITY_RANK else "info",
            source=source,
            title=title,
            message=message,
            timestamp=time.time(),
            code=code,
        )
        self._events.append(event)
        for queue in list(self._listeners):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled client loses live events; it catches up via ?since=.
                pass
        self._forward_to_ntfy(event)
        return event

    def events_since(self, since: int = 0, limit: int = BUFFER_SIZE) -> List[RelayEvent]:
        items = [event for event in self._events if event.seq > since]
        return items[-limit:] if limit > 0 else items

    def oldest_seq(self) -> int:
        return self._events[0].seq if self._events else self._seq

    def register_listener(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._listeners.add(queue)
        return queue

    def remove_listener(self, queue: asyncio.Queue) -> None:
        self._listeners.discard(queue)

    def status(self) -> Dict[str, Any]:
        return {
            "bootId": self.boot_id,
            "lastSeq": self._seq,
            "oldestSeq": self.oldest_seq(),
            "hubConnected": self.hub_connected,
            "listeners": len(self._listeners),
            "ntfyLastError": self.ntfy_last_error,
        }

    # -- configuration ---------------------------------------------------

    def update_config(self, update: Dict[str, Any]) -> Dict[str, Any]:
        self.config = merge_config_update(self.config, update)
        save_config(self.config, self.config_path)
        return self.config

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._run_hub_listener()),
            asyncio.create_task(self._run_system_checks()),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []

    # -- PINS hub --------------------------------------------------------

    def handle_hub_frame(self, frame: Dict[str, Any]) -> Optional[str]:
        """Handle one SignalR frame. Returns a reply to send, if any."""
        frame_type = frame.get("type")
        if frame_type == 6:  # ping
            return json.dumps({"type": 6}) + SIGNALR_RECORD_SEPARATOR
        if frame_type == 1 and frame.get("target") == "ReceiveNotification":
            if not self.config.get("includeNina", True):
                return None
            arguments = frame.get("arguments") or []
            fields = nina_notification_fields(arguments[0] if arguments else None)
            if fields:
                self.publish(source="nina", **fields)
        return None

    async def _run_hub_listener(self) -> None:
        try:
            from websockets.asyncio.client import connect
        except ImportError:  # pragma: no cover - dependency is pinned
            print("[notify] websockets client unavailable; PINS hub relay disabled")
            return

        delay = HUB_RECONNECT_MIN_SECONDS
        while True:
            try:
                async with connect(self.hub_url, open_timeout=10, ping_interval=None) as ws:
                    await ws.send(signalr_handshake())
                    self.hub_connected = True
                    delay = HUB_RECONNECT_MIN_SECONDS
                    print(f"[notify] connected to PINS notification hub {self.hub_url}")
                    async for payload in ws:
                        if isinstance(payload, bytes):
                            payload = payload.decode("utf-8", errors="replace")
                        for frame in parse_signalr_frames(payload):
                            if frame.get("type") == 7:  # close
                                raise ConnectionError(frame.get("error") or "hub closed")
                            reply = self.handle_hub_frame(frame)
                            if reply:
                                await ws.send(reply)
            except asyncio.CancelledError:
                self.hub_connected = False
                raise
            except Exception as exc:
                if self.hub_connected:
                    print(f"[notify] PINS notification hub disconnected: {exc}")
            self.hub_connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, HUB_RECONNECT_MAX_SECONDS)

    # -- host events -----------------------------------------------------

    async def check_system_once(self) -> None:
        if not self.config.get("includeSystem", True):
            return

        if self.power_probe is not None:
            try:
                power = await self.power_probe()
            except Exception:
                power = None
            if power is not None:
                under_voltage = bool(power.get("underVoltage"))
                if under_voltage and not self._system.under_voltage:
                    voltage = power.get("supplyVoltage")
                    detail = f" ({voltage:.2f} V)" if isinstance(voltage, (int, float)) else ""
                    self.publish(
                        "error",
                        "system",
                        f"Under-voltage detected on the Raspberry Pi{detail}. "
                        "Cameras and USB devices may disconnect.",
                        title="Power supply",
                        code="system.underVoltage",
                    )
                elif not under_voltage and self._system.under_voltage:
                    self.publish(
                        "success", "system", "Supply voltage is back to normal.",
                        title="Power supply", code="system.underVoltageCleared",
                    )
                self._system.under_voltage = under_voltage

        if self.temperature_probe is not None:
            try:
                celsius = await self.temperature_probe()
            except Exception:
                celsius = None
            if celsius is not None:
                threshold = float(self.config["temperatureThresholdC"])
                hot = celsius >= threshold
                # 5 °C hysteresis so a value hovering at the limit does not spam.
                cooled = celsius < threshold - 5.0
                if hot and not self._system.over_temperature:
                    self.publish(
                        "warning", "system",
                        f"CPU temperature is {celsius:.1f} °C (limit {threshold:.0f} °C).",
                        title="Temperature", code="system.overTemperature",
                    )
                    self._system.over_temperature = True
                elif cooled and self._system.over_temperature:
                    self._system.over_temperature = False

        threshold_gb = float(self.config["diskFreeThresholdGb"])
        if threshold_gb > 0:
            try:
                free_gb = shutil.disk_usage(self.config["diskPath"]).free / 1024 ** 3
            except OSError:
                free_gb = None
            if free_gb is not None:
                low = free_gb < threshold_gb
                if low and not self._system.low_disk:
                    self.publish(
                        "error", "system",
                        f"Only {free_gb:.1f} GB free on {self.config['diskPath']}. "
                        "New images may fail to save.",
                        title="Disk space", code="system.lowDisk",
                    )
                    self._system.low_disk = True
                elif free_gb >= threshold_gb * 1.2 and self._system.low_disk:
                    self._system.low_disk = False

    async def _run_system_checks(self) -> None:
        while True:
            try:
                await self.check_system_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[notify] system check failed: {exc}")
            await asyncio.sleep(SYSTEM_CHECK_INTERVAL_SECONDS)

    # -- ntfy ------------------------------------------------------------

    def _forward_to_ntfy(self, event: RelayEvent) -> None:
        ntfy = self.config.get("ntfy") or {}
        if not ntfy.get("enabled") or not ntfy.get("url"):
            return
        if not severity_at_least(event.severity, ntfy.get("minSeverity", "warning")):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.run_in_executor(None, self._send_ntfy, dict(ntfy), event)

    def ntfy_request(self, ntfy: Dict[str, Any], event: RelayEvent) -> urllib.request.Request:
        priority = {"error": "5", "warning": "4", "success": "3", "info": "3"}[event.severity]
        tags = {"error": "rotating_light", "warning": "warning", "success": "white_check_mark", "info": "telescope"}
        title = event.title or "PINS"
        headers = {
            # HTTP headers must be latin-1; ntfy decodes RFC 2047 encoded titles.
            "Title": _rfc2047(title),
            "Priority": priority,
            "Tags": tags[event.severity],
        }
        if ntfy.get("token"):
            headers["Authorization"] = f"Bearer {ntfy['token']}"
        return urllib.request.Request(
            ntfy["url"], data=event.message.encode("utf-8"), headers=headers, method="POST"
        )

    def _send_ntfy(self, ntfy: Dict[str, Any], event: RelayEvent) -> None:
        try:
            request = self.ntfy_request(ntfy, event)
            with urllib.request.urlopen(request, timeout=NTFY_TIMEOUT_SECONDS) as response:
                response.read()
            self.ntfy_last_error = None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Expected whenever the Pi has no internet (hotspot mode).
            self.ntfy_last_error = str(exc)


def _rfc2047(value: str) -> str:
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        import base64

        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return f"=?UTF-8?B?{encoded}?="
