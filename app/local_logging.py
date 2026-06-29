import os
import re
import sys
import threading
from datetime import datetime, timedelta
from typing import Optional, TextIO


LOG_DIR = os.getenv("PINSDAEMON_LOG_DIR", "/opt/pinsdaemon/logs")
LOG_RETENTION_DAYS = max(1, int(os.getenv("PINSDAEMON_LOG_RETENTION_DAYS", "5")))
_LOCK = threading.Lock()
_REDACTIONS = [
    re.compile(r"(\bpassword\s+)(\"[^\"]*\"|\S+)", re.IGNORECASE),
    re.compile(r"(\bwifi-sec\.psk\s+)(\"[^\"]*\"|\S+)", re.IGNORECASE),
    re.compile(r"(\bAuthorization:\s*Bearer\s+)(\S+)", re.IGNORECASE),
    re.compile(r"(\btoken=)([^&\s]+)", re.IGNORECASE),
]
_CATEGORY_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_stdio_installed = False


def redact_log_line(line: str) -> str:
    redacted = line
    for pattern in _REDACTIONS:
        redacted = pattern.sub(r"\1***", redacted)
    return redacted


def _safe_category(category: str) -> str:
    candidate = _CATEGORY_RE.sub("-", category.strip())
    return candidate or "pinsdaemon"


def _log_path(category: str, now: Optional[datetime] = None) -> str:
    current = now or datetime.now()
    return os.path.join(LOG_DIR, f"{_safe_category(category)}-{current:%Y-%m-%d}.log")


def prune_old_logs() -> None:
    cutoff = datetime.now() - timedelta(days=LOG_RETENTION_DAYS - 1)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        for name in os.listdir(LOG_DIR):
            match = re.match(r"^[A-Za-z0-9_.-]+-(\d{4}-\d{2}-\d{2})\.log$", name)
            if not match:
                continue
            try:
                log_date = datetime.strptime(match.group(1), "%Y-%m-%d")
            except ValueError:
                continue
            if log_date.date() < cutoff.date():
                os.remove(os.path.join(LOG_DIR, name))
    except Exception:
        pass


def append_local_log(category: str, line: str) -> None:
    if not line:
        return

    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    message = redact_log_line(str(line).rstrip("\n"))
    try:
        with _LOCK:
            os.makedirs(LOG_DIR, exist_ok=True)
            prune_old_logs()
            with open(_log_path(category), "a", encoding="utf-8") as log_file:
                log_file.write(f"{timestamp} {message}\n")
    except Exception:
        pass


class _TeeStream:
    def __init__(self, wrapped: TextIO, category: str):
        self._wrapped = wrapped
        self._category = category
        self._buffer = ""

    def write(self, text: str) -> int:
        written = self._wrapped.write(text)
        self._wrapped.flush()
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            append_local_log(self._category, line)
        return written

    def flush(self) -> None:
        self._wrapped.flush()
        if self._buffer:
            append_local_log(self._category, self._buffer)
            self._buffer = ""

    def isatty(self) -> bool:
        return self._wrapped.isatty()

    @property
    def encoding(self) -> str | None:
        return self._wrapped.encoding


def install_stdio_tee() -> None:
    global _stdio_installed
    if _stdio_installed:
        return
    _stdio_installed = True
    prune_old_logs()
    sys.stdout = _TeeStream(sys.stdout, "pinsdaemon")
    sys.stderr = _TeeStream(sys.stderr, "pinsdaemon")
    append_local_log("pinsdaemon", "pinsdaemon local logging initialized")
