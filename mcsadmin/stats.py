"""CPU and memory monitoring for the machine and the server process.

Derived from /proc (Linux) with graceful fallbacks, so there are no
external dependencies. On non-Linux platforms the values degrade to
None rather than crashing the TUI.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional


class SystemStats:
    """Polling object for system and per-process CPU/RAM."""

    def __init__(self) -> None:
        self._prev_cpu: Optional[Dict[str, int]] = None
        self._proc_jiffies: Optional[int] = None
        self._proc_at: Optional[float] = None
        self.jiffy_hz = self._clock_ticks()

    # ------------------------------------------------------------------
    @staticmethod
    def _clock_ticks() -> int:
        try:
            import os

            return os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        except Exception:
            return 100

    # ------------------------------------------------------------------
    @staticmethod
    def _cpu_times() -> Optional[Dict[str, int]]:
        try:
            with open("/proc/stat", "r") as fh:
                line = fh.readline()
            parts = line.split()
            if not parts or parts[0] != "cpu":
                return None
            keys = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
            return {k: int(v) for k, v in zip(keys, parts[1:9])}
        except Exception:
            return None

    def system_cpu(self) -> Optional[float]:
        t = self._cpu_times()
        if t is None:
            return None
        if self._prev_cpu is not None and t != self._prev_cpu:
            prev = self._prev_cpu
            idle = (t["idle"] + t["iowait"]) - (prev["idle"] + prev["iowait"])
            total = sum(t.values()) - sum(prev.values())
            self._prev_cpu = t
            if total <= 0:
                return 0.0
            return max(0.0, min(100.0, (1.0 - idle / total) * 100.0))
        self._prev_cpu = t
        return None

    # ------------------------------------------------------------------
    @staticmethod
    def system_mem() -> Dict[str, Optional[int]]:
        """Return (total, available) bytes, or None values."""
        try:
            meminfo = {}
            with open("/proc/meminfo", "r") as fh:
                for line in fh:
                    parts = line.split(":")
                    if len(parts) == 2:
                        val = parts[1].strip().split()[0]
                        meminfo[parts[0]] = int(val) * 1024  # kB -> bytes
            total = meminfo.get("MemTotal")
            avail = meminfo.get("MemAvailable") or meminfo.get("MemFree")
            return {"total": total, "available": avail}
        except Exception:
            return {"total": None, "available": None}

    # ------------------------------------------------------------------
    def process_cpu(self, pid: int) -> Optional[float]:
        """CPU percentage of pid since last call. One-privileges-basis: ticks."""
        jiffies = self._process_total_jiffies(pid)
        if jiffies is None:
            return None
        now = time.monotonic()
        prev_j = self._proc_jiffies
        prev_at = self._proc_at
        self._proc_jiffies = jiffies
        self._proc_at = now
        if prev_j is None or prev_at is None:
            return None
        dt = now - prev_at
        dj = jiffies - prev_j
        if dt <= 0 or dj < 0:
            return 0.0
        pct = (dj / self.jiffy_hz) / dt * 100.0
        return max(0.0, min(100.0, pct))

    @staticmethod
    def _process_total_jiffies(pid: int) -> Optional[int]:
        try:
            with open(f"/proc/{pid}/stat", "r") as fh:
                raw = fh.read()
            # comm can contain spaces; find last ')' then everything after is well-ordered.
            idx = raw.rfind(")")
            if idx < 0:
                return None
            fields = raw[idx + 1 :].split()
            # Field 14 (utime) and 15 (stime) relative to field
            # 3 == start. After ')' we have fields 3..52, index 11-12.
            utime = int(fields[11])
            stime = int(fields[12])
            cutime = int(fields[13])
            cstime = int(fields[14])
            return utime + stime + cutime + cstime
        except Exception:
            return None

    # ------------------------------------------------------------------
    @staticmethod
    def process_mem(pid: int) -> Optional[int]:
        """Resident set size in bytes (VmRSS)."""
        try:
            with open(f"/proc/{pid}/status", "r") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        return int(parts[1]) * 1024  # kB -> bytes
        except Exception:
            pass
        return None


# Minimal process tree helper: find Java processes by cmdline marker.
JAVA_MARKERS = ("server.jar", "minecraft_server", "-jar")


def find_server_pid(scan_cmdline: Optional[str] = None) -> Optional[int]:
    """Return the PID of a running MC server, if any."""
    import os

    marker = scan_cmdline or "server.jar"
    for dirpath, dirnames, filenames in os.walk("/proc"):
        del dirnames, filenames
        pid_dir = dirpath.rsplit("/", 1)[-1]
        if not pid_dir.isdigit():
            continue
        try:
            with open(os.path.join(dirpath, "cmdline"), "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if marker in cmd:
            return int(pid_dir)
    return None


def find_all_server_pids() -> List[int]:
    import os

    out = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "-jar" in cmd and any(m in cmd for m in JAVA_MARKERS):
            out.append(int(entry))
    return out