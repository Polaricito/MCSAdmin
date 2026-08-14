"""Server process control: launch, stop, restart, console I/O.

Launches the vanilla server.jar, streams its stdout into a shared
LogBuffer, parses player join/leave lines for an offline fallback player
list, and provides RCON-backed command execution and player queries.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

from .config import Config
from .rcon import RCONClient, parse_player_list
from .util import Event, LogBuffer, is_running, offline_uuid, terminate_process

MAX_RE = re.compile(r"There are \d+ of a max of (\d+) players online")
WHITELIST_RE = re.compile(r"There are \d+ whitelisted players: (.*)")
# vanilla logs the connecting address: "[Server thread/INFO]: Steve[/192.168.1.5:54321] logged in with entity id …"
IPLOG_RE = re.compile(r"([A-Za-z0-9_]+)\[([^\]]+):\d+\] logged in with entity id")


def read_properties(path: str) -> Dict[str, str]:
    """Parse a server.properties file into a {key: value} dict."""
    result: Dict[str, str] = {}
    try:
        with open(path, "r") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    result[k.strip()] = v.strip()
    except OSError:
        pass
    return result


def set_property(path: str, key: str, value: str) -> None:
    """Set one key in a properties file, preserving the other lines.

    Used for single switches (e.g. white-list) without rewriting the whole
    file; creates the file with just that key if it does not exist yet."""
    lines = []
    try:
        with open(path, "r") as fh:
            lines = fh.readlines()
    except OSError:
        pass
    pattern = re.compile(r"^" + re.escape(key) + r"\s*=")
    found = False
    for i, line in enumerate(lines):
        if pattern.match(line.lstrip()):
            lines[i] = f"{key}={value}\n"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}\n")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        fh.writelines(lines)


def parse_whitelist(resp: str) -> List[str]:
    """Names from a ``whitelist list`` response, e.g. 'There are 2 whitelisted players: A, B'."""
    if not resp:
        return []
    m = WHITELIST_RE.search(resp)
    if not m:
        return []
    return [n.strip() for n in m.group(1).split(",") if n.strip()]


def read_whitelist_file(path: str) -> List[str]:
    """Player names from a vanilla whitelist.json (usable while stopped)."""
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    names = []
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and entry.get("name"):
                names.append(entry["name"])
    return names


def write_whitelist_file(path: str, names: List[str]) -> None:
    """Write whitelist.json; names get valid offline-mode UUIDs."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    entries = [{"uuid": offline_uuid(n), "name": n} for n in names]
    with open(path, "w") as fh:
        json.dump(entries, fh)


def add_whitelist_entry(path: str, name: str) -> bool:
    names = read_whitelist_file(path)
    if name in names:
        return False
    names.append(name)
    write_whitelist_file(path, names)
    return True


def remove_whitelist_entry(path: str, name: str) -> bool:
    names = read_whitelist_file(path)
    if name not in names:
        return False
    names.remove(name)
    write_whitelist_file(path, names)
    return True


class ServerManager:
    """Owns the java subprocess and its threads."""

    def __init__(self, config: Config, log: LogBuffer) -> None:
        self.config = config
        self.log = log
        self.proc: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.started_at: Optional[float] = None
        self.status = "stopped"  # stopped | starting | running | stopping
        self.last_message: str = ""

        # player tracking
        self.players: Set[str] = set()
        self.player_ips: Dict[str, str] = {}
        self.max_players: Optional[int] = None
        self.players_lock = threading.Lock()
        self.rcon: Optional[RCONClient] = None
        self.rcon_ready = False

        # events
        self.on_status = Event("status")
        self.on_player_change = Event("players")
        self.on_stats = Event("stats")

        self._io_thread: Optional[threading.Thread] = None
        self._rcon_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._manual_stop = False

    # ------------------------------------------------------------------
    def setup_files(self) -> None:
        """Write eula.txt and server.properties that our tool needs."""
        server_dir = self._server_dir()
        os.makedirs(server_dir, exist_ok=True)
        eula = os.path.join(server_dir, "eula.txt")
        if os.path.exists(eula):
            with open(eula, "r") as fh:
                if "eula=true" in fh.read():
                    pass
        else:
            with open(eula, "w") as fh:
                fh.write("eula=true\n")

        props = os.path.join(server_dir, "server.properties")
        self._write_properties(props)
        self._load_max_players()

    def _load_max_players(self) -> None:
        """max-players from server.properties (vanilla default is 20)."""
        props = read_properties(os.path.join(self._server_dir(), "server.properties"))
        try:
            self.max_players = int(props.get("max-players", 20))
        except (TypeError, ValueError):
            self.max_players = 20

    def _write_properties(self, path: str) -> None:
        defaults = {
            "server-port": str(self.config.get("gameport", 25565)),
            "motd": self.config.get("motd", "MCSAdmin managed server"),
            "level-name": str(self.config.get("level") or "world"),
            "online-mode": "true",
            "spawn-protection": "0",
            "view-distance": "10",
            "enable-rcon": "true",
            "rcon.password": str(self.config.rcon.get("password") or ""),
            "rcon.port": str(self.config.rcon.get("port", 25575)),
        }
        existing = read_properties(path)
        # settings we manage always reflect the current config (e.g. a new
        # motd set from the settings screen must be written, not ignored)
        existing.update(defaults)
        # Netty's native transport aborts startup on old versions (they don't
        # ship the epoll library); force it off in server.properties. If it is
        # already set to false we leave it alone rather than rewriting it.
        current_netty = existing.get("use-native-transport")
        if current_netty is None or current_netty.strip().lower() != "false":
            existing["use-native-transport"] = "false"
        # world options managed from the config (World Options menu) always win;
        # anything not listed there keeps its existing value
        existing.update(
            {k: str(v) for k, v in (self.config.get("world") or {}).items()}
        )
        lines = ["#Minecraft server properties\n", "#Generated by MCSAdmin\n"]
        for k, v in sorted(existing.items()):
            lines.append(f"{k}={v}\n")
        with open(path, "w") as fh:
            fh.writelines(lines)

    # ------------------------------------------------------------------
    def _server_dir(self) -> str:
        return self.config.server_dir()

    def _resolve_java(self) -> Optional[str]:
        """Choose the java binary: config override > managed JRE > PATH."""
        from . import javavm

        override = self.config.get("java") or {}
        if override.get("path") and os.path.exists(override["path"]):
            return override["path"]
        managed = os.path.join(self._server_dir(), ".vms")
        if os.path.isdir(managed):
            for entry in sorted(os.listdir(managed), reverse=True):
                cand = os.path.join(managed, entry, "bin", "java")
                if os.path.exists(cand):
                    return cand
        return javavm.system_java_bin()

    def _java_flags(self, java: Optional[str] = None) -> List[str]:
        flags = []
        mx = self.config.java.get("max_memory_mb", 2048)
        mn = self.config.java.get("min_memory_mb", 1024)
        if mx:
            flags.append(f"-Xmx{mx}M")
        if mn:
            flags.append(f"-Xms{mn}M")
        cores = self.config.java.get("cores")
        if cores:
            flags.append(f"-XX:ActiveProcessorCount={int(cores)}")
        # JEP 412/454 native-access: newer JDKs warn (or refuse) when JNI
        # code isn't allowed native access. The option is only understood by
        # Java 17+, so gate it on the detected JVM to keep old servers
        # (1.8.9-era Java 8) bootable.
        if java:
            from . import javavm

            major = javavm.installed_java_major(java)
            if major is not None and major >= 17:
                flags.append("--enable-native-access=ALL-UNNAMED")
        extra = (self.config.java.get("extra") or "").strip()
        if extra:
            flags.extend(extra.split())
        flags.append("-jar")
        flags.append("server.jar")
        flags.append("nogui")
        return flags

    def _check_java_compat(self, java: str) -> Tuple[bool, str]:
        """Warn when the chosen JVM is too old for this jar."""
        from . import javavm

        jar = os.path.join(self._server_dir(), "server.jar")
        # Always recompute from the jar that actually sits in server_dir.
        # The "required" value cached in the config reflects the version that
        # (finally) installed a managed JRE; trusting it after switching to
        # another version (e.g. a downgrade to 1.8.9) wrongly refuses to start.
        required, _src = javavm.required_java_major(jar, None)
        installed = javavm.installed_java_major(java)
        if installed is None:
            return True, "Could not determine JVM version."
        if installed < required:
            return False, (
                f"Installed Java {installed} is too old; this server needs Java "
                f"{required}+. Run 'mcsadmin install --with-java' to fetch one."
            )
        return True, ""

    # ------------------------------------------------------------------
    def start(self) -> bool:
        if self.proc and self.proc.poll() is None:
            return False
        self._manual_stop = False
        self._stop_flag.clear()
        java = self._resolve_java()
        if not java:
            self.status = "stopped"
            self.last_message = "Java not found. Install a JRE (17+)."
            self.on_status.fire(self.status, self.last_message)
            return False

        self.setup_files()
        server_dir = self._server_dir()
        jar = os.path.join(server_dir, "server.jar")
        if not os.path.exists(jar):
            self.status = "stopped"
            self.last_message = "server.jar missing. Run 'mcsadmin install'."
            self.on_status.fire(self.status, self.last_message)
            return False

        ok, note = self._check_java_compat(java)
        if not ok:
            self.status = "stopped"
            self.last_message = note
            self.log.append(f"[mcsadmin] {note}")
            self.on_status.fire(self.status, note)
            return False
        if note:
            self.log.append(f"[mcsadmin] {note}")

        cmd = [java] + self._java_flags(java)
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=server_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
            )
        except OSError as exc:
            self.status = "stopped"
            self.last_message = f"Failed to launch java: {exc}"
            self.on_status.fire(self.status, self.last_message)
            return False

        self.pid = self.proc.pid
        self.started_at = time.time()
        self.status = "starting"
        self.log.clear()
        self.on_status.fire(self.status, "Starting server…")

        self._io_thread = threading.Thread(target=self._io_loop, daemon=True)
        self._io_thread.start()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        return True

    # ------------------------------------------------------------------
    def _io_loop(self) -> None:
        # snapshot the stream so a concurrent _cleanup() (which nulls
        # self.proc) can't race 'self.proc.stdout' and crash with
        # AttributeError: 'NoneType' object has no attribute 'stdout'
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        stdout = proc.stdout
        for line in iter(stdout.readline, ""):
            if self._stop_flag.is_set():
                break
            self._handle_line(line.rstrip("\n"))
        try:
            stdout.close()
        except OSError:
            pass

    def _handle_line(self, raw: str) -> None:
        self.log.append(raw)
        self._detect_player_events(raw)
        m = MAX_RE.search(raw)
        if m:
            with self.players_lock:
                try:
                    self.max_players = int(m.group(1))
                except ValueError:
                    pass
        lower = raw.lower()
        if self.status == "starting" and "done" in lower and "for help" in lower:
            self.status = "running"
            self.last_message = "Server ready."
            self.on_status.fire(self.status, self.last_message)

    def _detect_player_events(self, raw: str) -> None:
        """Track joins/leaves from vanilla console lines.

        Lines carry a ``[time] [thread/INFO]: `` prefix, so the name is
        matched anywhere (not anchored to the line start).
        """
        plain = raw
        if plain.endswith("joined the game"):
            m = re.search(r"([A-Za-z0-9_]+) joined the game$", plain)
            if m:
                with self.players_lock:
                    self.players.add(m.group(1))
                    self.on_player_change.fire(sorted(self.players))
            ip = IPLOG_RE.search(plain)
            if ip:
                with self.players_lock:
                    self.player_ips[ip.group(1)] = ip.group(2).lstrip("/")
        elif plain.endswith("left the game"):
            m = re.search(r"([A-Za-z0-9_]+) left the game$", plain)
            if m:
                with self.players_lock:
                    self.players.discard(m.group(1))
                    self.player_ips.pop(m.group(1), None)
                    self.on_player_change.fire(sorted(self.players))
        else:
            # connection line carries the address: Name[/ip:port] logged in …
            ip = IPLOG_RE.search(plain)
            if ip:
                with self.players_lock:
                    self.players.add(ip.group(1))
                    self.player_ips[ip.group(1)] = ip.group(2).lstrip("/")
                    self.on_player_change.fire(sorted(self.players))

    # ------------------------------------------------------------------
    def _monitor_loop(self) -> None:
        ticks = 0
        while not self._stop_flag.is_set():
            time.sleep(1.0)
            ticks += 1
            proc = self.proc
            if proc is None:
                continue
            if proc.poll() is not None:
                self._on_exit(proc.poll())
                break
            # poll the player list via RCON every 5s, not on every tick
            if self.status == "running" and ticks % 5 == 0:
                self._poll_rcon_players()

    def _poll_rcon_players(self) -> None:
        if not self.config.rcon.get("enabled", True):
            return
        try:
            if not self.rcon_ready:
                self._rcon_login()
            if self.rcon_ready and self.rcon:
                resp = self.rcon.command("list")
                if resp:
                    m = MAX_RE.search(resp)
                    if m:
                        with self.players_lock:
                            try:
                                self.max_players = int(m.group(1))
                            except ValueError:
                                pass
                    names = parse_player_list(resp)
                    with self.players_lock:
                        if names:
                            self.players = set(names)
                        # keep locally-detected names if rcon returns empty
                        self.player_ips = {
                            k: v for k, v in self.player_ips.items() if k in self.players
                        }
                        self.on_player_change.fire(sorted(self.players))
        except Exception:
            self.rcon_ready = False
            try:
                if self.rcon:
                    self.rcon.close()
            except Exception:
                pass

    def _rcon_login(self) -> None:
        port = int(self.config.rcon.get("port", 25575))
        password = str(self.config.rcon.get("password") or "")
        if not password:
            return
        client = RCONClient(port=port, password=password)
        try:
            client.login()
        except Exception:
            client.close()
            return
        self.rcon = client
        self.rcon_ready = True

    # ------------------------------------------------------------------
    def status_text(self) -> Dict[str, str]:
        return {
            "status": self.status,
            "pid": str(self.pid or "-"),
            "uptime": (
                f"{int(time.time() - self.started_at)}s"
                if self.started_at
                else "-"
            ),
            "players": str(len(self.players)),
        }

    def send_command(self, cmd: str) -> bool:
        if not cmd.strip():
            return True
        if self.proc is None or self.proc.poll() is not None:
            self.log.append("[mcsadmin] Server is not running — ignoring command.")
            return False
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
            self.log.append(f"[mcsadmin] > {cmd}")
            return True
        except OSError:
            return False

    def rcon_command(self, cmd: str) -> Optional[str]:
        """Run a command over RCON and return the response (or None)."""
        if not self.rcon_ready or self.rcon is None:
            return None
        try:
            return self.rcon.command(cmd)
        except Exception:  # noqa: BLE001
            return None

    def stop(self, graceful: bool = True) -> bool:
        if self.proc is None or self.proc.poll() is not None:
            self._cleanup()
            return True
        self._manual_stop = True
        self.status = "stopping"
        self.on_status.fire(self.status, "Stopping server…")
        if graceful:
            self.send_command("stop")
            deadline = time.time() + 25.0
            while time.time() < deadline:
                proc = self.proc
                if proc is None or proc.poll() is not None:
                    break
                time.sleep(0.2)
        proc = self.proc
        if proc is not None and proc.poll() is None:
            self._force_kill()
        self._cleanup()
        return True

    def _force_kill(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.send_signal(signal.SIGTERM)
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                self.proc.kill()
            except OSError:
                pass

    def _cleanup(self) -> None:
        self._stop_flag.set()
        with self.players_lock:
            self.players.clear()
            self.player_ips.clear()
            self.max_players = None
        if self.rcon:
            try:
                self.rcon.close()
            except Exception:
                pass
        self.rcon = None
        self.rcon_ready = False
        self.pid = None
        self.proc = None
        self.started_at = None
        self.status = "stopped"

    def _on_exit(self, _code: int) -> None:
        self._cleanup()
        if not self._manual_stop:
            self.last_message = "Server exited unexpectedly."
            self.on_status.fire(self.status, self.last_message)

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        try:
            self.stop(graceful=True)
        except Exception:
            self._cleanup()