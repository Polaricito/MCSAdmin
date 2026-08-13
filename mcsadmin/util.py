"""Small shared helpers."""

from __future__ import annotations

import os
import re
import signal
import socket
import threading
import time
from typing import Iterable, Optional


def fmt_bytes(n: float) -> str:
    """Format a byte count for humans (whole values drop the '.0')."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            text = f"{n:.1f}"
            if text.endswith(".0"):
                text = text[:-2]
            return f"{text} {unit}"
        n /= 1024.0
    return f"{n:.1f} PiB"


def fmt_seconds(seconds: float) -> str:
    """Format a duration as HH:MM:SS (or MM:SS)."""
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def truncate(text: str, width: int) -> str:
    if width <= 1:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def offline_uuid(name: str) -> str:
    """Vanilla 'offline-mode' UUIDv3 for a player name (OfflinePlayer:name),
    as Java's UUID.nameUUIDFromBytes produces. Lets a stopped server's
    whitelist.json gain valid entries without an account lookup."""
    import hashlib

    md5 = bytearray(hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest())
    md5[6] = (md5[6] & 0x0F) | 0x30  # version 3
    md5[8] = (md5[8] & 0x3F) | 0x80  # RFC 4122 variant
    h = md5.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def fetch_public_ip(timeout: float = 3.0) -> Optional[str]:
    """Return this host's public (WAN) IP, or the outbound interface IP.

    Queries ipify over HTTPS (the app is already network-dependent for
    downloads); if that fails it falls back to the address of the local
    socket a packet to the internet would leave from, and finally None.
    """
    import urllib.request

    try:
        req = urllib.request.Request(
            "https://api.ipify.org", headers={"User-Agent": "MCSAdmin/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ip = resp.read().decode("ascii").strip()
        if _IP_RE.match(ip):
            return ip
    except Exception:  # noqa: BLE001
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:  # noqa: BLE001
        return None


class LogBuffer:
    """Thread-safe bounded list of console lines."""

    def __init__(self, maxlen: int = 3000) -> None:
        self.maxlen = maxlen
        self._lines: list = []
        self._lock = threading.Lock()
        self.count = 0

    def append(self, line) -> None:
        with self._lock:
            self._lines.append(line)
            self.count += 1
            if len(self._lines) > self.maxlen:
                del self._lines[: len(self._lines) - self.maxlen]

    def extend(self, lines: Iterable) -> None:
        for line in lines:
            self.append(line)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self.count = 0

    def tail(self, count: int) -> list:
        with self._lock:
            return list(self._lines[-count:])

    def total(self) -> int:
        return self.count


class Event:
    """A lightweight pub/sub event."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self, fn) -> None:
        with self._lock:
            self._subs.append(fn)

    def fire(self, *args, **kwargs) -> None:
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(*args, **kwargs)
            except Exception:
                pass


def is_running(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


def download_with_progress(
    url: str,
    dest: str,
    progress=None,
    timeout: float = 300.0,
    chunk_size: int = 256 * 1024,
) -> int:
    """Stream a URL to a file, reporting progress as (done, total)."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "MCSAdmin/1.0"})
    tmp = dest + ".part"
    written = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = None
        length = resp.headers.get("Content-Length")
        if length:
            total = int(length)
        with open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
                if progress:
                    progress(written, total or 0)
            fh.flush()
            os.fsync(fh.fileno())
    os.replace(tmp, dest)
    return written


def terminate_process(pid: Optional[int], timeout: float = 8.0) -> bool:
    """SIGTERM then SIGKILL. Returns True once gone."""
    if not is_running(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running(pid):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    time.sleep(0.1)
    return not is_running(pid)