"""The curses TUI.

Responsive layout: when the terminal is wide, console occupies the left
and players+stats sit on the right; when narrow, everything stacks
vertically. The console uses a lighter shade, the player list a darker
one, matching the theme in theme.py.
"""

from __future__ import annotations

import curses
import os
import sys
import threading
import time
from typing import List, Optional

from . import __version__, APP_TITLE
from .config import Config
from .server import (
    ServerManager,
    add_whitelist_entry,
    parse_whitelist,
    read_properties,
    read_whitelist_file,
    remove_whitelist_entry,
    set_property,
)
from .stats import SystemStats, find_server_pid
from .theme import theme
from .util import LogBuffer, clamp, fmt_bytes, fmt_seconds, truncate
from .versions import (
    download_async,
    fetch_manifest,
    read_installed_version,
)

KEY_ENTER = 10
KEY_ESCAPE = 27
KEY_BACKSPACE = 263
KEY_DELETE = 330
KEY_PGUP = 339
KEY_PGDN = 338
KEY_UP = 259
KEY_DOWN = 258

HELP_TEXT = [
    "MCSAdmin key bindings:",
    "  type + Enter ..... send a server command or '/command'",
    "  S ................ start the server",
    "  X ................ stop the server",
    "  R ................ restart the server",
    "  I ................ install the latest release build",
    "  V ................ pick a version to install",
    "  W ................ world options (difficulty, gamemode, pvp, …)",
    "  E ................ server settings (description / icon / RAM / cores)",
    "  H ................ show this screen",
    "  Q / Ctrl-Q ....... quit (stops server if running)",
    "  PgUp/PgDn ........ scroll the console",
    "",
    "  click a footer button to start/stop/restart/install/etc.",
    "  click a player in the player list to kick or ban them",
    "",
    "Shortcuts are UPPERCASE so you can type any server command",
    "freely; lowercase input is never treated as a hotkey.",
    "",
    "Local control commands (inside the console):",
    "  /start  /stop  /restart     server lifecycle",
    "  /install [version]          install server.jar",
    "  /versions                   list available versions",
    "  /help                       show this screen",
    "  anything else               sent straight to the server console",
]


class VersionModal:
    """Modal list of Minecraft versions with a search filter."""

    def __init__(self, server_dir: str) -> None:
        self.versions: List[str] = []
        self.query = ""
        self.sel = 0
        self.loading = True
        self.error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self.server_dir = server_dir
        self.installed: Optional[str] = read_installed_version(server_dir)

    def filtered(self) -> List[str]:
        q = self.query.strip().lower()
        if not q:
            return self.versions
        return [v for v in self.versions if q in v.lower()]

    def open(self) -> None:
        self.loading = True
        self.error = None
        self.versions = []
        self.query = ""
        self.sel = 0
        self.installed = read_installed_version(self.server_dir)
        self._thread = threading.Thread(target=self._fetch, daemon=True)
        self._thread.start()

    def _fetch(self) -> None:
        try:
            m = fetch_manifest()
            releases = [v.id for v in m.versions if v.type == "release"]
            snaps = [v.id for v in m.versions if v.type == "snapshot"]
            self.versions = releases + snaps
            self.installed = read_installed_version(self.server_dir)
            self.loading = False
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.loading = False


class PlayerActions:
    """Small modal offering kick/ban/IP-ban for a player."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.actions: List[str] = [
            "Kick player", "Ban player", "IP ban", "Cancel",
        ]
        self.sel = 0


class WhitelistModal:
    """Server-wide whitelist editor: name list, a bottom button bar."""

    def __init__(self, query_fn, enabled: bool = True) -> None:
        self.query_fn = query_fn
        self.enabled = enabled
        self.names: List[str] = []
        self.loading = True
        self.error: Optional[str] = None
        self.editing = False
        self.buf = ""
        self.sel = 0
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        try:
            self.names = self.query_fn()
            self.error = None
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        finally:
            self.loading = False

    def toggle_label(self) -> str:
        return "Disable whitelist" if self.enabled else "Enable whitelist"

    def actions(self) -> List[str]:
        return list(self.names) + ["Add", self.toggle_label(), "Done"]


class SettingsModal:
    """Modal to edit the server description (motd), icon and JVM
    settings (RAM / cores). World options have their own 'W' key."""

    def __init__(self, config: Config, server_dir: str) -> None:
        self.actions: List[str] = [
            "description", "icon", "java settings", "done",
        ]
        self.motd = (config.get("motd") or "").strip() or "MCSAdmin managed server"
        self.sel = 0
        self.editing: Optional[str] = None  # None | "description" | "icon"
        self.icon_src: Optional[str] = config.get("server_icon")
        self.icons = self._find_icons(server_dir)
        self.icon_i = (
            self.icons.index(self.icon_src)
            if self.icon_src and self.icon_src in self.icons
            else -1
        )
        # A path typed/pasted by the user takes priority over the cycle.
        self.custom_icon: Optional[str] = None
        self.icon_error: str = ""
        # True once the user actively cycles/commits an icon; lets saving
        # keep the existing icon when the field was left alone (icon_i can be
        # -1 because the current icon isn't among the discovered candidates).
        self.icon_touched = False
        jf = config.java
        # assigned RAM is the max heap; the stored min is ignored (kept for
        # backward compatibility, cleared on the next save).
        self.ram = jf.get("max_memory_mb") or jf.get("min_memory_mb")
        self.cores = jf.get("cores")

    @staticmethod
    def _find_icons(server_dir: str) -> List[str]:
        found: List[str] = []
        for root in (server_dir, os.path.expanduser("~"), os.getcwd()):
            try:
                for name in sorted(os.listdir(root)):
                    if name.lower().endswith((".png", ".jpg", ".jpeg")):
                        found.append(os.path.join(root, name))
            except OSError:
                pass
        return found

    def current_icon(self) -> str:
        if self.custom_icon:
            return os.path.basename(self.custom_icon)
        if 0 <= self.icon_i < len(self.icons):
            return os.path.basename(self.icons[self.icon_i])
        if self.icon_src:
            return os.path.basename(self.icon_src)
        return "none"

    def cycle_icon(self) -> None:
        self.icon_touched = True
        self.custom_icon = None
        n = len(self.icons)
        if n == 0:
            self.icon_i = -1
            return
        # -1 == none, then wraps through each candidate
        self.icon_i = (self.icon_i + 1) % (n + 1) - 1

    def commit_icon(self) -> None:
        """Validate the typed icon path and adopt it (or clear it)."""
        self.icon_touched = True
        path = (self.custom_icon or "").strip()
        self.icon_error = ""
        if not path:
            self.custom_icon = None
            self.icon_src = None
            self.icon_i = -1
            return
        if not os.path.isfile(os.path.expanduser(path)):
            self.icon_error = f"not a file: {path}"
            return
        self.custom_icon = os.path.expanduser(path)
        self.icon_src = self.custom_icon
        self.icon_i = -1


# (config/property key, label, kind) for the JVM and world option editors.
# kind: "int" (typed digits, blank = auto), "bool" (cycle true/false),
#       "choice:a,b,c" (cycle). A "default" value means "leave unchanged".
# The assigned RAM is the maximum heap (-Xmx); the min heap is pinned to it.
JVM_FIELDS = [
    ("ram_mb", "assigned ram (MiB)", "int"),
    ("cores", "cpu cores (blank = auto)", "int"),
]

WORLD_FIELDS = [
    ("difficulty", "difficulty", "choice:peaceful,easy,normal,hard"),
    ("gamemode", "gamemode", "choice:survival,creative,adventure,spectator"),
    ("max-players", "max players", "int"),
    ("view-distance", "view distance", "int"),
    ("pvp", "pvp", "bool"),
    ("hardcore", "hardcore", "bool"),
    ("spawn-monsters", "spawn monsters", "bool"),
    ("spawn-animals", "spawn animals", "bool"),
    ("enable-command-block", "command blocks", "bool"),
    ("spawn-protection", "spawn protection (radius)", "int"),
    ("generate-structures", "generate structures", "bool"),
    ("allow-flight", "allow flight", "bool"),
    ("white-list", "white-list", "bool"),
    ("online-mode", "online mode", "bool"),
    ("whitelist", "whitelist", "action"),
]


class FieldModal:
    """A scrollable list of editable fields (JVM settings / World Options).

    ``values`` maps each field key to its current value (int for "int"
    fields, True/False for "bool", a chosen option string for "choice",
    None meaning "use default / auto"). Navigating past the last field
    reaches a "done" row that commits via ``on_save``.
    """

    def __init__(self, title, fields, values, on_save) -> None:
        self.title = title
        self.fields = fields  # list of (key, label, kind)
        self.values: dict = dict(values)
        self.on_save = on_save  # callable(dict) -> None (also closes the modal)
        self.sel = 0
        self.editing: Optional[int] = None  # index of the field being typed
        self.edit_buf: str = ""

    def scroll_start(self, rows: int) -> int:
        """First field index to display given ``rows`` visible field rows."""
        return max(0, min(self.sel, len(self.fields) - 1) - rows + 1)


class App:
    def __init__(self, stdscr, config: Config) -> None:
        self.stdscr = stdscr
        self.config = config
        self.log = LogBuffer()
        self.server = ServerManager(config, self.log)
        self.console_offset: Optional[int] = None  # None == follow tail
        self.input_buf: str = ""
        self.input_pos: int = 0
        self.input_mode = "console"  # 'console' | 'modal'
        self.modal: Optional[object] = None
        self.prev_modal: Optional[object] = None  # parent of a sub-menu
        self.message = ""  # status line message
        self._message_at = 0.0
        self._hitmap: dict = {}  # (y, x) -> action token from the last frame
        self._pane_origin: dict = {}  # pane name -> (top_left_y, top_left_x)

        # monitoring state
        self.stats = SystemStats()
        self.sys_cpu: Optional[float] = None
        self.sys_mem = {"total": None, "available": None}
        self.srv_cpu: Optional[float] = None
        self.srv_mem: Optional[int] = None
        self.public_ip: Optional[str] = None
        self.ext_pid: Optional[int] = find_server_pid()
        if self.ext_pid:
            self.log.append(f"[mcsadmin] Detected running server (PID {self.ext_pid}).")

        # install state
        self.install_active = False
        self.install_progress = (0, 0)
        self.last_result = ""
        self.installed_ver: Optional[str] = None

        self.last_lines = ""
        self._quit_requested = False

        # pane/dirty-tracking state
        self._panes = {}          # name -> curses window for each panel
        self._modal_win = None
        self._modal_was_open = False
        self._dirty = set()       # pane names pending redraw
        self._need_rebuild = False
        self._small = False
        self._last_console = (0, None)
        self._last_players: tuple = ()
        self._last_status: tuple = ()
        self._last_stats_at = 0.0
        self._last_input_at = 0.0

    # ==================================================================
    # run loop
    # ==================================================================
    _IDLE_SLEEP = 0.03
    _STATS_INTERVAL = 0.8
    _MESSAGE_MS = 5.0
    _HOTKEYS = frozenset("SXRIVWHEQ")  # all hotkeys are UPPERCASE

    def run(self) -> None:
        self.stdscr.keypad(True)
        self.stdscr.nodelay(True)
        curses.curs_set(1)
        theme.init(self.stdscr)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except curses.error:
            pass
        server_dir = self.config.server_dir()
        self.installed_ver = read_installed_version(server_dir)
        if self.installed_ver:
            self.log.append(
                f"[mcsadmin] Minecraft {self.installed_ver} installed; "
                f"files in {server_dir}"
            )
        if not os.path.exists(os.path.join(self.config.server_dir(), "server.jar")):
            self.log.append("[mcsadmin] No server.jar installed. Press 'I' to install,"
                           " or 'V' to pick a version.")
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        self._rebuild_panes()
        while True:
            try:
                self._pump()
                try:
                    ch = self.stdscr.getch()
                except KeyboardInterrupt:
                    ch = 17  # Ctrl-Q
                if ch == curses.KEY_RESIZE:
                    # curses.update_lines_cols() makes ncurses adopt the new
                    # size and CLEARS the internal resize flag. Calling only
                    # curses.resizeterm() here can return ERR (which our old
                    # code swallowed), leaving the flag set so getch() keeps
                    # returning KEY_RESIZE on every frame — each one triggering
                    # a full pane rebuild/repaint. That repaint storm floods
                    # the terminal, the pty output buffer back-pressures, and
                    # the app blocks in doupdate(): the "stuck UI" you see.
                    # Also: never call refresh() here — it flushes a frame
                    # where the panes still use the old geometry (the flicker).
                    try:
                        curses.update_lines_cols()
                    except Exception:  # noqa: BLE001
                        try:
                            curses.endwin()
                            self.stdscr.refresh()
                        except curses.error:
                            pass
                    self._need_rebuild = True
                    continue
                if ch == curses.KEY_MOUSE:
                    if self._handle_mouse():
                        break
                    continue
                if ch == -1:
                    time.sleep(self._IDLE_SLEEP)
                    continue
                if self._quit_requested:
                    break
                if not self._process_key(ch):
                    break
                self._mark("input")
            except Exception:  # noqa: BLE001
                # never let a single bad frame kill the TUI and leave the
                # terminal in a broken state; log it and keep rendering
                self.log.append("[mcsadmin] internal error (see traceback)")
                self.log.append(repr(sys.exc_info()[1]))
                self._mark("console")
                self._mark("footer")
                continue
        self.shutdown()

    # ------------------------------------------------------------------
    # monitoring thread
    # ------------------------------------------------------------------
    def _monitor_loop(self) -> None:
        ip_tick = 0
        while True:
            time.sleep(1.0)
            cpu = self.stats.system_cpu()
            if cpu is not None:
                self.sys_cpu = cpu
            self.sys_mem = self.stats.system_mem()
            if self.public_ip is None:
                ip_tick += 1
                if ip_tick % 5 == 0:  # don't hammer a network call every second
                    try:
                        from .util import fetch_public_ip

                        self.public_ip = fetch_public_ip()
                    except Exception:  # noqa: BLE001
                        self.public_ip = None
            pid = self.server.pid or self.ext_pid
            if pid:
                sc = self.stats.process_cpu(pid)
                if sc is not None:
                    self.srv_cpu = sc
                sm = self.stats.process_mem(pid)
                if sm is not None:
                    self.srv_mem = sm
            else:
                self.srv_cpu = None
                self.srv_mem = None

    # ------------------------------------------------------------------
    # key routing
    # ------------------------------------------------------------------
    def handle_key(self, ch: int) -> bool:
        if self.modal is not None:
            return self._handle_modal_key(ch)
        if self.input_buf:
            return self._edit_key(ch)
        return self._hotkey(ch)

    def _process_key(self, ch: int) -> bool:
        return self.handle_key(ch)

    def _hotkey(self, ch: int) -> bool:
        """Empty-input keys (scroll/navigation/enter/uppercase hotkeys)."""
        if ch in (KEY_UP, KEY_PGUP):
            self._scroll(-20)
            return True
        if ch in (KEY_DOWN, KEY_PGDN):
            self._scroll(20)
            return True
        if ch in (13, 10, KEY_ENTER):
            self._submit()
            return True
        if 32 <= ch < 127 and chr(ch) in self._HOTKEYS:
            return self._run_hotkey(ch)
        return self._edit_key(ch)

    def _server_running(self) -> bool:
        proc = getattr(self.server, "proc", None)
        return proc is not None and proc.poll() is None

    def _run_hotkey(self, ch: int) -> bool:
        """Execute an UPPERCASE hotkey. False == quit."""
        if ch in (ord("Q"), 17):  # Q / Ctrl-Q
            return False
        if ch == ord("I"):
            self._quick_install()
        elif ch == ord("V"):
            self._open_version_modal()
        elif ch == ord("E"):
            if self._server_running():
                self._notify("Stop server first.")
            else:
                self._open_settings_modal()
        elif ch == ord("W"):
            if self._server_running():
                self._notify("Stop server first.")
            else:
                self._open_world_modal()
        elif ch == ord("S"):
            if self._server_running():
                self._notify("Server already running.")
            else:
                self.server.start()
        elif ch == ord("X"):
            if self._server_running():
                self.server.stop()
            else:
                self._notify("Server isn't running.")
        elif ch == ord("R"):
            if self._server_running():
                self.server.stop()
                self.server.start()
            else:
                self.server.start()
        elif ch == ord("H"):
            self.log.extend(HELP_TEXT)
        return True

    def _edit_key(self, ch: int) -> bool:
        if ch == KEY_BACKSPACE:
            if self.input_pos > 0:
                self.input_buf = (
                    self.input_buf[: self.input_pos - 1]
                    + self.input_buf[self.input_pos:]
                )
                self.input_pos -= 1
            return True
        if ch == KEY_DELETE:
            if self.input_pos < len(self.input_buf):
                self.input_buf = (
                    self.input_buf[: self.input_pos]
                    + self.input_buf[self.input_pos + 1:]
                )
            return True
        if ch == curses.KEY_LEFT:
            self.input_pos = max(0, self.input_pos - 1)
            return True
        if ch == curses.KEY_RIGHT:
            self.input_pos = min(len(self.input_buf), self.input_pos + 1)
            return True
        if ch == curses.KEY_HOME:
            self.input_pos = 0
            return True
        if ch == curses.KEY_END:
            self.input_pos = len(self.input_buf)
            return True
        if ch == 13 or ch == KEY_ENTER:
            self._submit()
            return True
        if ch in (KEY_UP, KEY_PGUP):
            self._scroll(-20)
            return True
        if ch in (KEY_DOWN, KEY_PGDN):
            self._scroll(20)
            return True
        if 32 <= ch < 127:
            self._edit_insert(ch)
        return True

    def _edit_insert(self, ch: int) -> bool:
        self.input_buf = (
            self.input_buf[: self.input_pos] + chr(ch) + self.input_buf[self.input_pos:]
        )
        self.input_pos += 1
        return True

    def _scroll(self, delta: int) -> None:
        n = self.log.total()
        visible = self._console_visible()
        if self.console_offset is None:
            base = n
        else:
            base = self.console_offset
        target = clamp(base + delta, 0, max(0, n - visible))
        self.console_offset = None if target >= max(0, n - visible) else target

    def _submit(self) -> None:
        text = self.input_buf.strip()
        self.input_buf = ""
        self.input_pos = 0
        if not text:
            return
        if text.startswith("/"):
            self._control_thinking(text)
        else:
            self.server.send_command(text)

    # Local control verbs
    def _control_thinking(self, text: str) -> None:
        cmd = text.lower()
        tokens = text.split()
        verb = tokens[0].lower()
        if verb in ("/start",):
            self.server.start()
        elif verb in ("/stop",):
            self.server.stop()
        elif verb in ("/restart",):
            self.server.stop()
            self.server.start()
        elif verb in ("/install",):
            version = tokens[1] if len(tokens) > 1 else None
            self._quick_install(version)
        elif verb in ("/version", "/versions"):
            self._open_version_modal()
        elif verb in ("/help", "/h"):
            self.log.extend(HELP_TEXT)
        elif verb in ("/exit", "/quit", "/logout"):
            self._quit_requested = True
        elif verb in ("/players", "/pl"):
            names = sorted(self.server.players)
            self.log.append(
                f"[mcsadmin] Players ({len(names)}): "
                + (", ".join(names) if names else "none")
            )
        else:
            self.server.send_command(text[1:])  # strip leading slash

    def _handle_modal_key(self, ch: int) -> bool:
        m = self.modal
        if m is None:
            return True
        if isinstance(m, FieldModal):
            return self._handle_field_key(m, ch)
        if isinstance(m, WhitelistModal):
            return self._handle_whitelist_key(m, ch)
        if ch in (KEY_ESCAPE,):
            self.modal = None
            return True
        if isinstance(m, PlayerActions):
            if ch == KEY_UP:
                m.sel = max(0, m.sel - 1)
            elif ch == KEY_DOWN:
                m.sel = min(len(m.actions) - 1, m.sel + 1)
            elif ch in (KEY_ENTER, 13):
                self._run_player_action(m, m.actions[m.sel])
            return True
        if isinstance(m, SettingsModal):
            if m.editing is not None:
                if m.editing == "description":
                    if ch == KEY_BACKSPACE:
                        m.motd = m.motd[:-1]
                    elif ch in (KEY_ENTER, 13, KEY_ESCAPE):
                        m.editing = None
                    elif 32 <= ch < 127:
                        m.motd += chr(ch)
                else:  # icon path
                    buf = m.custom_icon or ""
                    if ch == KEY_BACKSPACE:
                        m.custom_icon = buf[:-1]
                    elif ch in (KEY_ENTER, 13, KEY_ESCAPE):
                        m.commit_icon()
                        m.editing = None
                    elif 32 <= ch < 127:
                        m.custom_icon = buf + chr(ch)
                return True
            if ch == KEY_UP:
                m.sel = max(0, m.sel - 1)
            elif ch == KEY_DOWN:
                m.sel = min(len(m.actions) - 1, m.sel + 1)
            elif ch in (KEY_ENTER, 13):
                if m.sel == 0:
                    m.editing = "description"
                elif m.sel == 1:
                    m.editing = "icon"
                elif m.sel == 2:
                    self._open_java_modal()
                elif m.sel == 3:
                    self._apply_settings(m)
            return True
        # VersionModal: typing edits the search filter
        if ch == KEY_BACKSPACE:
            m.query = m.query[:-1]
            m.sel = 0
            return True
        if ch == KEY_UP:
            m.sel = max(0, m.sel - 1)
            return True
        if ch == KEY_DOWN:
            f = m.filtered()
            m.sel = min(len(f) - 1, m.sel + 1) if f else 0
            return True
        if ch in (KEY_ENTER, 13):
            if not m.loading and not m.error:
                f = m.filtered()
                if f:
                    version = f[min(m.sel, len(f) - 1)]
                    self.modal = None
                    self._quick_install(version)
            return True
        if 32 <= ch < 127:
            m.query += chr(ch)
            m.sel = 0
        return True

    def _run_player_action(self, m: PlayerActions, action: str) -> None:
        name = m.name
        if action.startswith("Kick"):
            self.modal = None
            self._notify(f"Kicking {name}…")
            self.server.send_command(f"kick {name}")
        elif action.startswith("Ban"):
            self.modal = None
            self._notify(f"Banning {name}…")
            self.server.send_command(f"ban {name}")
        elif action.startswith("IP ban"):
            self.modal = None
            ip = self.server.player_ips.get(name)
            if ip:
                self._notify(f"Banning {name} ({ip})…")
                self.server.send_command(f"ban-ip {ip}")
            else:
                self._notify(f"No connection IP known for {name}.")
        else:  # Cancel
            self.modal = None

    def _open_whitelist_modal(self) -> None:
        self.prev_modal = self.modal  # keep the world options underneath
        props = read_properties(
            os.path.join(self.config.server_dir(), "server.properties")
        )
        enabled = str(props.get("white-list", "true")).lower() in ("true", "1")
        self.modal = WhitelistModal(self._whitelist_query, enabled=enabled)

    def _whitelist_path(self) -> str:
        return os.path.join(self.config.server_dir(), "whitelist.json")

    def _whitelist_query(self) -> List[str]:
        # world options (and thus this editor) are only reachable while the
        # server is stopped, so read the on-disk whitelist.json instead of
        # demanding a live RCON connection.
        resp = self.server.rcon_command("whitelist list")
        if resp:
            return parse_whitelist(resp)
        return read_whitelist_file(self._whitelist_path())

    def _whitelist_add(self, m: WhitelistModal) -> None:
        name = m.buf.strip()
        m.buf = ""
        m.editing = False
        if not name:
            return
        if self._server_running():
            self.server.send_command(f"whitelist add {name}")
        else:
            add_whitelist_entry(self._whitelist_path(), name)
        self._notify(f"{name} added to whitelist.")
        threading.Thread(target=m._load, daemon=True).start()

    def _whitelist_remove(self, m: WhitelistModal, name: str) -> None:
        if self._server_running():
            self.server.send_command(f"whitelist remove {name}")
        else:
            remove_whitelist_entry(self._whitelist_path(), name)
        self._notify(f"{name} removed from whitelist.")
        threading.Thread(target=m._load, daemon=True).start()

    def _whitelist_toggle(self, m: WhitelistModal) -> None:
        """Flip white-list in server.properties, keeping the entries."""
        value = "false" if m.enabled else "true"
        set_property(
            os.path.join(self.config.server_dir(), "server.properties"),
            "white-list",
            value,
        )
        # keep the config's world overrides in sync so a later start doesn't
        # write the whitelist back the other way
        world = dict(self.config.get("world") or {})
        world["white-list"] = value
        self.config.set("world", world)
        self.config.save()
        m.enabled = not m.enabled
        self._notify(
            f"Whitelist {'enabled' if m.enabled else 'disabled'} (entries kept)."
        )

    def _handle_whitelist_key(self, m: WhitelistModal, ch: int) -> bool:
        if m.editing:
            if ch == KEY_BACKSPACE:
                m.buf = m.buf[:-1]
            elif ch in (KEY_ENTER, 13):
                self._whitelist_add(m)
            elif ch in (KEY_ESCAPE,):
                m.editing = False
                m.buf = ""
            elif 32 <= ch < 127:
                m.buf += chr(ch)
            return True
        if ch in (KEY_ESCAPE,):
            self.modal = self.prev_modal
            self.prev_modal = None
            return True
        if ch == KEY_UP:
            m.sel = max(0, m.sel - 1)
            return True
        if ch == KEY_DOWN:
            m.sel = min(len(m.actions()) - 1, m.sel + 1)
            return True
        if ch in (KEY_ENTER, 13):
            n = len(m.names)
            if m.sel == n:
                m.editing = True
                m.buf = ""
            elif m.sel == n + 1:
                self._whitelist_toggle(m)
            elif m.sel == n + 2:
                self.modal = self.prev_modal
                self.prev_modal = None
            else:
                self._whitelist_remove(m, m.names[m.sel])
        return True

    # ------------------------------------------------------------------
    # FieldModal (JVM settings / world options)
    # ------------------------------------------------------------------
    def _handle_field_key(self, m: FieldModal, ch: int) -> bool:
        if m.editing is not None:
            kind = m.fields[m.editing][2]
            if ch == KEY_BACKSPACE:
                m.edit_buf = m.edit_buf[:-1]
            elif ch in (KEY_ENTER, 13, KEY_ESCAPE):
                self._commit_field(m)
            elif 32 <= ch < 127:
                c = chr(ch)
                if kind == "int" and not c.isdigit():
                    return True
                m.edit_buf += c
            return True
        if ch == KEY_ESCAPE:
            self._close_field_modal()
            return True
        if ch == KEY_UP:
            m.sel = max(0, m.sel - 1)
        elif ch == KEY_DOWN:
            m.sel = min(len(m.fields), m.sel + 1)  # last index == "done"
        elif ch == curses.KEY_RIGHT:
            if m.sel < len(m.fields) and m.fields[m.sel][2] == "action":
                self._field_enter(m, m.sel)
        elif ch in (KEY_ENTER, 13):
            if m.sel == len(m.fields):
                m.on_save(dict(m.values))
            else:
                self._field_enter(m, m.sel)
        return True

    def _close_field_modal(self) -> None:
        """Close a sub-menu, returning to its parent (e.g. the settings
        screen) rather than dropping all the way back to the main view."""
        if self.prev_modal is not None:
            self.modal = self.prev_modal
            self.prev_modal = None
        else:
            self.modal = None

    def _field_enter(self, m: FieldModal, idx: int) -> None:
        """Enter a field: cycle bool/choice, open an action, or start typing."""
        key, _label, kind = m.fields[idx]
        if kind == "action":
            self._open_whitelist_modal()
            return
        if kind == "bool":
            cur = m.values.get(key)
            if cur is None:
                m.values[key] = True
            else:
                m.values[key] = not (str(cur).lower() in ("true", "1"))
        elif kind.startswith("choice:"):
            opts = kind.split(":", 1)[1].split(",")
            cur = m.values.get(key)
            pos = opts.index(cur) if cur in opts else -1
            m.values[key] = opts[(pos + 1) % len(opts)]
        else:  # int / str: start editing
            v = m.values.get(key)
            m.edit_buf = "" if v is None else str(v)
            m.editing = idx

    def _commit_field(self, m: FieldModal) -> None:
        kind = m.fields[m.editing][2]
        buf = m.edit_buf.strip()
        if kind == "int":
            try:
                m.values[m.fields[m.editing][0]] = int(buf)
            except ValueError:
                m.values[m.fields[m.editing][0]] = None  # blank == auto
        else:
            m.values[m.fields[m.editing][0]] = buf or None
        m.editing = None
        m.edit_buf = ""

    @staticmethod
    def _field_value_text(kind: str, v) -> str:
        """Value text for a field row; None means default/auto."""
        if v is None or v == "":
            if kind.startswith("choice") or kind == "bool":
                return "default"
            return "auto"
        if kind == "int":
            return str(int(v))
        if kind == "bool":
            return "true" if str(v).lower() in ("true", "1") else "false"
        return str(v)

    def _field_row_text(self, m: FieldModal, idx: int, mw: int) -> str:
        """Row text for one FieldModal field (used by draw + hitmap)."""
        key, label, kind = m.fields[idx]
        prefix = " > " if idx == m.sel else "   "
        if kind == "action":
            # the whitelist editor is active only while white-list is on
            active = str(m.values.get("white-list")).lower() in ("true", "1")
            shown = truncate(label, mw - 4) + (" >" if active else "")
            return truncate(prefix + shown, mw - 4)
        if m.editing == idx:
            value = truncate(m.edit_buf + "_", mw - 15)
        else:
            value = self._field_value_text(kind, m.values.get(key))
        return truncate(
            prefix + truncate(label, mw - 16) + ": " + value, mw - 4
        )

    def _open_java_modal(self) -> None:
        self.prev_modal = self.modal
        jf = self.config.java
        self.modal = FieldModal(
            " JAVA SETTINGS ",
            JVM_FIELDS,
            {
                "ram_mb": jf.get("max_memory_mb") or jf.get("min_memory_mb"),
                "cores": jf.get("cores"),
            },
            on_save=self._save_java_settings,
        )

    def _save_java_settings(self, values: dict) -> None:
        jf = self.config.java
        if "ram_mb" in values:
            # the assigned RAM is the maximum heap; pin the min heap to it so
            # the JVM doesn't start small and grow slowly
            ram = values["ram_mb"]
            jf["max_memory_mb"] = ram
            jf["min_memory_mb"] = ram
        if "cores" in values:
            jf["cores"] = values["cores"]
        self.config.save()
        self._notify("RAM / cores saved (applied on next start).")
        self._close_field_modal()

    def _open_world_modal(self) -> None:
        self.prev_modal = self.modal
        props = read_properties(
            os.path.join(self.config.server_dir(), "server.properties")
        )
        world = dict(self.config.get("world") or {})
        values = {}
        for key, _label, kind in WORLD_FIELDS:
            v = world.get(key, props.get(key))
            if kind == "int":
                try:
                    values[key] = int(str(v))
                except (TypeError, ValueError):
                    values[key] = None
            elif kind == "bool":
                if v is None:
                    values[key] = None
                else:
                    values[key] = str(v).strip().lower() == "true"
            else:
                values[key] = None if v is None else str(v)
        self.modal = FieldModal(
            " WORLD OPTIONS ", WORLD_FIELDS, values, on_save=self._save_world_options
        )

    def _save_world_options(self, values: dict) -> None:
        cfg = {}
        for key, _label, kind in WORLD_FIELDS:
            v = values.get(key)
            if v is None or v == "":
                continue
            if kind == "bool":
                v = "true" if str(v).lower() in ("true", "1") else "false"
            cfg[key] = str(v)
        self.config.set("world", cfg)
        try:
            self.server.setup_files()
        except Exception:  # noqa: BLE001
            pass
        self._notify("World options saved (applied on start).")
        self._close_field_modal()

    # ------------------------------------------------------------------
    # settings row rendering (shared by draw + hitmap)
    # ------------------------------------------------------------------
    def _settings_row(self, m: SettingsModal, i: int, w: int):
        """(text, token) for one SettingsModal row, mirroring its drawing."""
        prefix = " > " if i == m.sel else "   "
        label = m.actions[i]
        if label == "description":
            text = prefix + "desc: " + truncate(m.motd, w - 13)
            if m.editing == "description" and i == m.sel:
                text += "_"
            return text, "settings:motd"
        if label == "icon":
            if m.editing == "icon":
                shown = truncate(m.custom_icon or "", w - 11)
                text = prefix + "path: " + shown
                if i == m.sel:
                    text += "_"
            else:
                shown = truncate(m.current_icon(), w - 11)
                text = prefix + "icon: " + shown
            if m.icon_error:
                text = truncate(text + "  ! " + m.icon_error, w)
            return text, "settings:icon"
        if label == "java settings":
            value = f"{m.ram or '-'} MiB"
            if m.cores:
                value += f", {m.cores} cores"
            return prefix + "jvm: " + truncate(value, w - 12), "settings:java"
        return prefix + "save", "settings:done"

    # ------------------------------------------------------------------
    # mouse
    # ------------------------------------------------------------------
    def _hit(self, y: int, x0: int, n: int, token: str) -> None:
        for x in range(max(0, x0), max(0, x0) + max(0, n)):
            self._hitmap[(y, x)] = token

    def _handle_mouse(self) -> bool:
        """Process a mouse event. Returns True to quit."""
        try:
            _id, mx, my, _z, bstate = curses.getmouse()
        except curses.error:
            return False
        if not (bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED)):
            return False
        if my < 0 or mx < 0:
            return False
        token = self._hitmap.get((my, mx))
        if token:
            return self._dispatch_click(token)
        return False

    def _dispatch_click(self, token: str) -> bool:
        if token == "quit":
            return True
        if token == "start":
            if not self._server_running():
                self.server.start()
        elif token == "stop":
            if self._server_running():
                self.server.stop()
        elif token == "restart":
            if self._server_running():
                self.server.stop()
                self.server.start()
            else:
                self.server.start()
        elif token == "install":
            self._quick_install()
        elif token == "pick":
            self._open_version_modal()
        elif token == "settings":
            if self._server_running():
                self._notify("Stop server first.")
            else:
                self._open_settings_modal()
        elif token == "world":
            if self._server_running():
                self._notify("Stop server first.")
            else:
                self._open_world_modal()
        elif token == "help":
            self.log.extend(HELP_TEXT)
        elif token == "close":
            self.modal = None
        elif token.startswith("settings:"):
            action = token.split(":", 1)[1]
            m = self.modal
            if isinstance(m, SettingsModal):
                if action == "motd":
                    m.sel = 0
                    m.editing = "description"
                elif action == "icon":
                    m.sel = 1
                    m.editing = "icon"
                elif action == "java":
                    self._open_java_modal()
                elif action == "done":
                    if m.editing is not None:
                        if m.editing == "icon":
                            m.commit_icon()
                        m.editing = None
                    else:
                        self._apply_settings(m)
        elif token.startswith("field:"):
            arg = token.split(":", 1)[1]
            m = self.modal
            if isinstance(m, FieldModal):
                if arg == "done":
                    m.on_save(dict(m.values))
                else:
                    try:
                        idx = int(arg)
                    except ValueError:
                        return False
                    if 0 <= idx < len(m.fields):
                        m.sel = idx
                        self._field_enter(m, idx)
        elif token.startswith("player:"):
            name = token.split(":", 1)[1]
            self.modal = PlayerActions(name)
        elif token.startswith("kick:"):
            name = token.split(":", 1)[1]
            self.modal = None
            self._notify(f"Kicking {name}…")
            self.server.send_command(f"kick {name}")
        elif token.startswith("ban:"):
            name = token.split(":", 1)[1]
            self.modal = None
            self._notify(f"Banning {name}…")
            self.server.send_command(f"ban {name}")
        elif token.startswith("ipban:"):
            name = token.split(":", 1)[1]
            self.modal = None
            ip = self.server.player_ips.get(name)
            if ip:
                self._notify(f"Banning {name} ({ip})…")
                self.server.send_command(f"ban-ip {ip}")
            else:
                self._notify(f"No connection IP known for {name}.")
        elif token.startswith("whitelist-remove:"):
            name = token.split(":", 1)[1]
            m = self.modal
            if isinstance(m, WhitelistModal):
                self._whitelist_remove(m, name)
        elif token == "whitelist-add":
            m = self.modal
            if isinstance(m, WhitelistModal):
                m.editing = True
                m.buf = ""
        elif token == "whitelist-toggle":
            m = self.modal
            if isinstance(m, WhitelistModal):
                self._whitelist_toggle(m)
        elif token.startswith("vinstall:"):
            version = token.split(":", 1)[1]
            self.modal = None
            self._quick_install(version)
        return False

    # ==================================================================
    # install
    # ==================================================================
    def _quick_install(self, version: Optional[str] = None) -> None:
        if self.install_active:
            self._notify("Install already in progress.")
            return
        if self.server.proc and self.server.proc.poll() is None:
            self._notify("Stop the server before installing.")
            return
        self.last_result = ""
        self.install_active = True
        self.install_progress = (0, 0)
        self._notify(f"Installing {version or 'latest'}…")

        self.log.append(f"[mcsadmin] Downloading server {version or 'latest'}…")
        from . import versions as vmod

        vmod.download_async(
            self.config.server_dir(),
            version,
            on_progress=self._on_install_progress,
            on_done=self._on_install_done,
            with_java=True,
        )

    def _on_install_progress(self, done: int, total: int) -> None:
        self.install_progress = (done, total)

    def _on_install_done(self, ok: bool, version: Optional[str], result: str) -> None:
        self.install_active = False
        if ok:
            self.config.set("version", version)
            self.installed_ver = version
            self.last_result = f"Installed {version}."
            self.log.append(f"[mcsadmin] Installed {version}. Press 'S' to start.")
        else:
            self.last_result = f"Install failed: {result}"
            self.log.append(f"[mcsadmin] Error: {result}")
            self._notify(self.last_result)

    def _open_version_modal(self) -> None:
        self.modal = VersionModal(self.config.server_dir())
        self.install_active = False
        self.modal.open()

    def _open_settings_modal(self) -> None:
        self.modal = SettingsModal(self.config, self.config.server_dir())

    def _apply_settings(self, m: SettingsModal) -> None:
        if m.icon_error:
            self.modal = None
            self._notify(m.icon_error)
            return
        self.config.set(
            "motd", (m.motd.strip() if m.motd.strip() else "MCSAdmin managed server")
        )
        src = None
        if m.custom_icon:
            src = m.custom_icon
        elif 0 <= m.icon_i < len(m.icons):
            src = m.icons[m.icon_i]
        elif m.icon_touched:
            src = None  # user explicitly cycled to "none"
        else:
            src = m.icon_src  # untouched: keep the existing icon (may be None)
        self.config.set("server_icon", src)
        server_dir = self.config.server_dir()
        target = os.path.join(server_dir, "server-icon.png")
        if src:
            try:
                os.makedirs(server_dir, exist_ok=True)
                from . import iconutil

                err = iconutil.normalize_icon(os.path.expanduser(src), target)
                if err:
                    self.config.set("server_icon", None)
                    self.log.append(f"[mcsadmin] Server icon rejected: {err}")
                    self.log.append(
                        "[mcsadmin] Avoid the vanilla 64x64 server-icon.png "
                        "gotcha by using a PNG (any size); JPEGs need ImageMagick."
                    )
                    self._notify("Icon rejected.")
                else:
                    self.log.append(
                        f"[mcsadmin] Server icon set to {os.path.basename(src)} "
                        "(64x64)."
                    )
            except OSError as exc:
                self.log.append(f"[mcsadmin] Could not write icon: {exc}")
        elif os.path.exists(target):
            try:
                os.remove(target)
                self.log.append("[mcsadmin] Server icon cleared.")
            except OSError:
                pass
        try:
            self.server.setup_files()
        except Exception:  # noqa: BLE001
            pass
        self.modal = None
        self._notify("Server details saved (applied on start).")

    def _notify(self, msg: str) -> None:
        self.message = msg
        self._message_at = time.monotonic()

    def _footer_text(self) -> str:
        """Transient footer message, empty once it has expired."""
        if self.message and time.monotonic() - self._message_at < self._MESSAGE_MS:
            return self.message
        return ""

    # ==================================================================
    # layout / geometry helpers
    # ==================================================================
    def getmaxyx(self):
        return self.stdscr.getmaxyx()

    def _console_visible(self) -> int:
        lay = self._layout()
        try:
            return lay["panes"]["console"][3]
        except KeyError:
            return max(1, int(lay["body_h"] * 0.60))

    # ==================================================================
    # render
    # ==================================================================
    def _layout(self):
        """Return per-pane rectangles for the current terminal size.

        The three panes must always tile ``body_h`` rows exactly (and never
        overlap the header, footer or input line). On narrow terminals the
        old math floored each pane's minimum height, so for small bodies the
        stacks summed to more than ``body_h`` and the stats pane overlapped
        the console/footer with its own text.
        """
        h, w = self.getmaxyx()
        header, input_h, footer = 1, 1, 1
        body_h = max(1, h - header - input_h - footer)
        wide = w >= 112
        if wide:
            right_w = clamp(int(w * 0.30), 30, 46)
            right_w = min(right_w, max(1, w - 1))
            console_w = max(1, w - right_w)
            players_h = max(1, min(int(body_h * 0.55), body_h - 1))
            stats_h = max(1, body_h - players_h)
            panes = {
                "console": (header, 0, console_w, body_h),
                "players": (header, console_w, right_w, players_h),
                "stats": (header + players_h, console_w, right_w, stats_h),
            }
        else:
            console_w = w
            c_h = max(1, int(body_h * 0.60))
            c_h = min(c_h, body_h)
            rest = body_h - c_h
            if rest >= 2:
                p_h = max(1, int(rest * 0.5))
                p_h = min(p_h, rest - 1)
                s_h = rest - p_h
            elif rest == 1:
                p_h, s_h = 0, 1
            else:
                p_h, s_h = 0, 0
            panes = {
                "console": (header, 0, console_w, c_h),
                "players": (header + c_h, 0, console_w, p_h),
                "stats": (header + c_h + p_h, 0, console_w, s_h),
            }
        return {
            "h": h,
            "w": w,
            "header_h": header,
            "input_h": input_h,
            "footer_h": footer,
            "body_h": body_h,
            "wide": wide,
            "panes": panes,
            "footer_y": h - header - input_h,
            "input_y": h - input_h,
        }

    # ------------------------------------------------------------------
    # pane windows + dirty tracking
    # ------------------------------------------------------------------
    def _rebuild_panes(self) -> None:
        """(Re)create the panel windows after a resize.

        Must never raise: a failure here would leave ``_need_rebuild`` set
        and every subsequent frame would throw, freezing the TUI. Old
        windows are erased first so they can't ghost over the new layout.
        """
        try:
            for win in self._panes.values():
                try:
                    win.erase()
                except curses.error:
                    pass
        except Exception:  # noqa: BLE001
            pass
        self._panes.clear()
        self._modal_win = None
        try:
            self.stdscr.clear()
        except curses.error:
            pass
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            lay = self._layout()
        except Exception:  # noqa: BLE001
            self._small = True
            return
        h, w = lay["h"], lay["w"]
        if w < 40 or h < 10:
            self._small = True
            return
        self._small = False
        bg = {
            "console": theme.attr("console"),
            "players": theme.attr("players"),
            "stats": theme.attr("stats"),
        }
        for name, (y, x, cw, ch) in lay["panes"].items():
            if cw < 1 or ch < 1:
                continue
            try:
                win = curses.newwin(ch, cw, y, x)
                win.bkgd(" ", bg.get(name, 0))
                win.scrollok(False)
                self._panes[name] = win
                self._pane_origin[name] = (y, x)
            except curses.error:
                continue
        self._mark("console")
        self._mark("players")
        self._mark("stats")
        self._mark("header")
        self._mark("footer")
        self._mark("input")

    def _mark(self, name: str) -> None:
        self._dirty.add(name)

    @staticmethod
    def _modal_geom(m, h: int, w: int):
        """(mh, mw, my, mx) for the current modal, mirroring _draw_modal."""
        if isinstance(m, PlayerActions):
            mh = min(len(m.actions) + 4, max(10, h - 6))
            mw = clamp(int(w * 0.4), 22, 40)
        elif isinstance(m, SettingsModal):
            mh = min(len(m.actions) + 4, max(10, h - 6))
            mw = clamp(int(w * 0.5), 30, 60)
        elif isinstance(m, WhitelistModal):
            mh = min(len(m.actions()) + 6, max(14, h - 6))
            mw = clamp(int(w * 0.4), 28, 50)
        elif isinstance(m, FieldModal):
            mh = min(len(m.fields) + 5, max(14, h - 8))
            mw = clamp(int(w * 0.5), 34, 56)
        else:
            # Fixed height: the modal must not resize as the search filter
            # narrows the list (that looked like a sub-menu popping open on
            # every typed character, e.g. each '.' in "1.21.11").
            mh = min(26, max(14, h - 8))
            mw = clamp(int(w * 0.5), 24, 60)
        my = (h - mh) // 2
        mx = (w - mw) // 2
        return mh, mw, my, mx

    def _record_hits(self) -> None:
        """Repopulate the click hitmap on frames where nothing was redrawn."""
        self._hitmap.clear()
        h, w = self.getmaxyx()
        fy = h - 2
        if fy >= 0:
            s = ""
            x = 0
            for label, token in self._footer_buttons():
                piece = ("  " if s else " ") + label
                s += piece
                self._hit(fy, x + 1, max(0, len(piece) - 1), token)
                x += len(piece)
        oy, ox = self._pane_origin.get("players", (0, 0))
        pwin = self._panes.get("players")
        if pwin is not None:
            try:
                pw, ph = pwin.getmaxyx()[1], pwin.getmaxyx()[0]
            except curses.error:
                pw = ph = 0
            for i, name in enumerate(sorted(self.server.players)[: max(0, ph - 2)]):
                self._hit(oy + 1 + i, ox + 1, len(truncate(name, pw - 2)),
                          "player:" + name)
        m = self.modal
        if m is not None:
            _mh, _mw, my, mx = self._modal_geom(m, h, w)
            if isinstance(m, PlayerActions):
                tokens = [
                    "kick:" + m.name,
                    "ban:" + m.name,
                    "ipban:" + m.name,
                    "close",
                ]
                for i, label in enumerate(m.actions):
                    token = tokens[i]
                    self._hit(my + 1 + i, mx + 1, len(truncate(label, _mw - 4)), token)
            elif isinstance(m, SettingsModal):
                for i, _label in enumerate(m.actions):
                    text, token = self._settings_row(m, i, _mw)
                    self._hit(my + 1 + i, mx + 1, len(text) - 1, token)
            elif isinstance(m, WhitelistModal):
                names = m.names
                list_rows = max(1, _mh - 5)
                for i in range(list_rows):
                    if i >= len(names):
                        break
                    label = names[i]
                    self._hit(my + 1 + i, mx + 1, len(truncate(label, _mw - 4)),
                              "whitelist-remove:" + label)
                bar = _mh - 3
                x = 1
                buttons = m.actions()[len(names):]
                tokens = ["whitelist-add", "whitelist-toggle", "close"]
                for i, label in enumerate(buttons):
                    btext = "[" + label + "]"
                    self._hit(my + bar, mx + x, len(btext), tokens[i])
                    x += len(btext) + 1
            elif isinstance(m, FieldModal):
                rows = max(1, _mh - 3)
                start = m.scroll_start(rows)
                for i in range(rows):
                    idx = start + i
                    if idx >= len(m.fields):
                        break
                    text = self._field_row_text(m, idx, _mw)
                    self._hit(my + 1 + i, mx + 1, len(text) - 1, "field:" + str(idx))
                text = " > done" if m.sel == len(m.fields) else "   done"
                self._hit(my + rows + 1, mx + 1, len(text) - 1, "field:done")
            else:
                items = m.filtered()
                visible = max(0, _mh - 5)
                start = max(0, m.sel - visible + 1)
                for i in range(visible):
                    idx = start + i
                    if idx >= len(items):
                        break
                    item = items[idx]
                    mark = "✓" if item == m.installed else " "
                    text = "  " + mark + " " + truncate(item, _mw - 5)
                    self._hit(my + 2 + i, mx + 1, len(text) - 1, "vinstall:" + item)

    # ------------------------------------------------------------------
    # pump: detect changes and redraw only affected panes
    # ------------------------------------------------------------------
    def _pump(self) -> None:
        if self._need_rebuild:
            self._rebuild_panes()
            self._need_rebuild = False
        if self._small:
            self._draw_small()
            return
        self._hitmap.clear()
        now = time.monotonic()

        cnt = self.log.total()
        anchor = (cnt, self.console_offset)
        if anchor != self._last_console:
            self._last_console = anchor
            self._mark("console")

        sig = (tuple(sorted(self.server.players)), self.server.max_players)
        if sig != self._last_players:
            self._last_players = sig
            self._mark("players")

        st_sig = (
            self.server.status,
            self.server.pid,
            not not self.server.started_at,
            self.server.rcon_ready,
            self.public_ip,
            self.server.max_players,
        )
        full_sig = (st_sig, self._footer_text(), self.install_active)
        if full_sig != self._last_status:
            self._last_status = full_sig
            self._mark("header")
            self._mark("footer")

        if now - self._last_stats_at > self._STATS_INTERVAL:
            self._last_stats_at = now
            self._mark("stats")

        modal_open = self.modal is not None
        if not modal_open and self._modal_was_open:
            # repaint the panels the modal was covering
            self._modal_win = None
            for n in ("console", "players", "stats", "header", "footer"):
                self._mark(n)
        self._modal_was_open = modal_open

        if not self._dirty:
            self._record_hits()
            return

        # draw each dirty element
        if "header" in self._dirty:
            self._draw_header()
        if "console" in self._dirty:
            self._draw_console()
        if "players" in self._dirty:
            self._draw_players()
        if "stats" in self._dirty:
            self._draw_stats()
        if "footer" in self._dirty:
            self._draw_footer()
        if modal_open:
            self._draw_modal()

        # input line: redraw when it changed (keypress) or ~2x/s so the
        # text cursor re-homes after other panes repaint
        if not modal_open and (
            "input" in self._dirty or now - self._last_input_at > 0.5
        ):
            self._last_input_at = now
            self._draw_input()
        curses.doupdate()
        self._dirty.clear()

    # ------------------------------------------------------------------
    # drawing
    # ------------------------------------------------------------------
    def _draw_header(self) -> None:
        h, w = self.getmaxyx()
        if h < 1:
            return
        status = self.server.status
        st_attr = theme.attr(
            "status_ok" if status == "running" else (
                "status_warn" if status in ("starting", "stopping") else "status_err"
            )
        )
        try:
            self.stdscr.noutrefresh()
            self.stdscr.addstr(0, 0, " " * w, theme.attr("header"))
            ip = f"ip={self.public_ip or '-'}"
            status_text = f"{status.upper():>8} pid={self.server.pid or self.ext_pid or '-'}"
            right_w = len(ip) + 1 + len(status_text)
            left_w = max(0, w - right_w - 2)
            title = f" {APP_TITLE} v{__version__}"
            if self.installed_ver and left_w > len(title) + len(self.installed_ver) + 4:
                title += f"  mc={self.installed_ver}"
            self.stdscr.addstr(0, 0, truncate(title, left_w), theme.attr("header"))
            # the IP sits in the white header box like the title/version, with
            # the status + pid (their own attribute) after it
            x = max(0, w - right_w)
            self.stdscr.addstr(0, x, ip, theme.attr("header"))
            self.stdscr.addstr(0, x + len(ip) + 1, status_text, st_attr)
            self.stdscr.noutrefresh()
        except curses.error:
            pass

    def _draw_console(self) -> None:
        win = self._panes.get("console")
        if win is None:
            return
        try:
            w, chh = win.getmaxyx()[1], win.getmaxyx()[0]
        except curses.error:
            return
        win.erase()
        try:
            win.border()
        except curses.error:
            pass
        self._winline(win, 0, 2, truncate(" SERVER CONSOLE ", w - 4),
                      theme.attr("console_accent", curses.A_BOLD))
        lines = self.log.tail(self.log.maxlen)
        n = len(lines)
        vis = max(0, chh - 2)
        start = max(0, n - vis) if self.console_offset is None else max(
            0, min(self.console_offset, n - vis)
        )
        for i in range(vis):
            idx = start + i
            text = lines[idx] if idx < n else ""
            self._winline(win, 1 + i, 1, truncate(text, w - 2), self._line_attr(text))
        if self.install_active:
            done, total = self.install_progress
            pct = done / total * 100 if total else 0
            bar = "#" * (int(pct) // 2)
            label = f" download {pct:3.0f}%"
            self._winline(win, chh - 1, 1,
                          truncate(f"[{bar.ljust(50)}]{label}", w - 2),
                          theme.attr("status_warn"))
        win.noutrefresh()

    def _draw_players(self) -> None:
        win = self._panes.get("players")
        if win is None:
            return
        try:
            w, ph = win.getmaxyx()[1], win.getmaxyx()[0]
        except curses.error:
            return
        win.erase()
        try:
            win.border()
        except curses.error:
            pass
        cnt = len(self.server.players)
        mp = self.server.max_players
        if mp:
            players_title = f" PLAYERS [{cnt}/{mp}] "
        elif cnt:
            players_title = f" PLAYERS [{cnt}] "
        else:
            players_title = " PLAYERS "
        self._winline(win, 0, 2, truncate(players_title, w - 4),
                      theme.attr("players_accent", curses.A_BOLD))
        names = sorted(self.server.players)
        vis = max(0, ph - 2)
        oy, ox = self._pane_origin.get("players", (0, 0))
        if not names:
            self._winline(win, 1, 1,
                          "offline" if self.server.status != "running" else "no players",
                          theme.attr("dim"))
        else:
            for i in range(min(vis, len(names))):
                name = names[i]
                ip = self.server.player_ips.get(name)
                label = truncate(f"{name}  {ip}" if ip else name, w - 2)
                self._winline(win, 1 + i, 1, label, theme.attr("players_accent"))
                self._hit(oy + 1 + i, ox + 1, len(label), "player:" + name)
        win.noutrefresh()

    def _stats_rows(self) -> List[tuple]:
        """(label, value, fraction) rows for the resources pane.

        ``fraction`` in [0,1] when the row has a measurable fill (cpu/ram),
        else None for plain text rows. Uptime is handled separately so it
        can be pinned to the bottom of the pane.
        """
        rows: List[tuple] = []
        total = self.sys_mem.get("total")
        avail = self.sys_mem.get("available")
        used = max(0, total - avail) if (total and avail is not None) else total
        pid = self.server.pid or self.ext_pid
        if pid:
            sc = self.srv_cpu
            rows.append(("Server CPU",
                         f"{sc:.0f}%" if sc is not None else "…",
                         sc / 100.0 if sc is not None else None))
            # live RSS of the server process vs the assigned heap, not the
            # PC's total RAM
            sm = self.srv_mem
            cfg = getattr(self, "config", None)
            cfg_java = (getattr(cfg, "java", None) or {}) if cfg is not None else {}
            assigned_mb = (
                cfg_java.get("max_memory_mb")
                or cfg_java.get("min_memory_mb")
                or 2048
            )
            assigned = assigned_mb * 1024 * 1024
            frac = (sm / assigned) if (sm and assigned) else None
            rows.append(("Server RAM",
                         f"{fmt_bytes(sm)} / {fmt_bytes(assigned)}"
                         if (sm and assigned) else "…",
                         min(1.0, frac) if frac is not None else None))
        sc = self.sys_cpu
        rows.append(("System CPU",
                     f"{sc:.0f}%" if sc is not None else "—",
                     sc / 100.0 if sc is not None else None))
        rows.append(("System RAM",
                     f"{fmt_bytes(used)} / {fmt_bytes(total)}" if total else "—",
                     (used / total) if total else None))
        # note: the player count lives under the PLAYERS header now, not here
        return rows

    def _stats_line(self, label, value, frac, w: int) -> str:
        """Render one compact stats row; fraction rows get a fill bar."""
        content = max(1, w - 2)
        if frac is None:
            return truncate(f"{label}: {value}", content)
        # give the value a fair slice, then let the bar fill the rest
        value_w = min(len(value), max(1, (content - len(label) - 4) // 2))
        shown = value if len(value) <= value_w else value[: value_w - 1] + "…"
        bar_w = content - len(label) - len(shown) - 3
        bar_w = max(4, bar_w)
        fill = int(round(frac * bar_w))
        bar = "#" * fill + "-" * (bar_w - fill)
        return truncate(f"{label} {bar} {shown}", content)

    def _bar_line(self, label, frac, w: int) -> str:
        """First line of a resource row: 'label [####----]'."""
        content = max(1, w - 2)
        inner = max(1, content - len(label) - 4)
        fill = int(round(frac * inner))
        return truncate(f"{label} [{'#' * fill}{'-' * (inner - fill)}]", content)

    def _bar_only(self, frac, w: int) -> str:
        """Just the fill bar, e.g. '[####--------]' (label/value are separate rows)."""
        content = max(1, w - 2)
        inner = max(1, content - 2)
        fill = int(round(frac * inner))
        return f"[{'#' * fill}{'-' * (inner - fill)}]"

    @staticmethod
    def _centered(value: str, w: int) -> str:
        """Second line of a resource row: the value, centered under the bar."""
        content = max(1, w - 2)
        v = truncate(value, content)
        return " " * ((content - len(v)) // 2) + v

    def _draw_stats(self) -> None:
        win = self._panes.get("stats")
        if win is None:
            return
        try:
            w, sh = win.getmaxyx()[1], win.getmaxyx()[0]
        except curses.error:
            return
        win.erase()
        try:
            win.border()
        except curses.error:
            pass
        self._winline(win, 0, 2, truncate(" RESOURCES ", w - 4),
                      theme.attr("stats_accent", curses.A_BOLD))
        uptime = (
            fmt_seconds(time.time() - self.server.started_at)
            if self.server.started_at
            else None
        )
        # reserve the bottom two content rows for the uptime block:
        #   15:48        <- value on top
        #   Uptime       <- label underneath
        uptime_rows = 2 if uptime else 0
        content_rows = max(1, sh - 2)
        top_limit = max(0, content_rows - uptime_rows)

        y = 1
        for label, value, frac in self._stats_rows():
            avail = top_limit - (y - 1)
            if avail <= 0:
                break
            if frac is not None and avail >= 3:
                # three-line form: bar on top, name below, value underneath, e.g.:
                #   [######----------]
                #   Server RAM
                #   900 MiB / 4 GiB
                self._winline(win, y, 1, self._bar_only(frac, w),
                              theme.attr("stats"))
                self._winline(win, y + 1, 1, self._centered(label, w),
                              theme.attr("dim"))
                self._winline(win, y + 2, 1, self._centered(value, w),
                              theme.attr("stats"))
                y += 3
            elif frac is not None and avail >= 2:
                # bar on top, value centered underneath, e.g.:
                #   server cpu [#######----------]
                #                  14%
                self._winline(win, y, 1, self._bar_line(label, frac, w),
                              theme.attr("stats"))
                self._winline(win, y + 1, 1, self._centered(value, w),
                              theme.attr("dim"))
                y += 2
            else:
                # not enough vertical room: compact single-line fallback
                self._winline(win, y, 1, self._stats_line(label, value, frac, w),
                              theme.attr("stats"))
                y += 1

        if uptime and sh >= 4:
            label_y = sh - 2  # last content row
            self._winline(win, label_y, 1, self._centered("Uptime", w),
                          theme.attr("dim"))
            self._winline(win, label_y - 1, 1, self._centered(uptime, w),
                          theme.attr("stats"))
        win.noutrefresh()

    def _footer_buttons(self) -> List[tuple]:
        """Context-sensitive clickable footer buttons: (label, token)."""
        running = self._server_running()
        if running:
            return [
                ("[X] stop", "stop"),
                ("[R] restart", "restart"),
                ("[H] help", "help"),
                ("[Q] quit", "quit"),
            ]
        return [
            ("[S] start", "start"),
            ("[I] install", "install"),
            ("[E] settings", "settings"),
            ("[W] world", "world"),
            ("[V] pick version", "pick"),
            ("[H] help", "help"),
            ("[Q] quit", "quit"),
        ]

    def _draw_footer(self) -> None:
        h, w = self.getmaxyx()
        y = h - 2
        if y < 0:
            return
        s = ""
        x = 0
        for label, token in self._footer_buttons():
            piece = ("  " if s else " ") + label
            s += piece
            self._hit(y, x + 1, max(0, len(piece) - 1), token)
            x += len(piece)
        s = truncate(s, w)
        try:
            self.stdscr.noutrefresh()
            self.stdscr.addstr(y, 0, " " * w, theme.attr("dim"))
            self.stdscr.addstr(y, 0, self._footer_text() or s, theme.attr("dim"))
            self.stdscr.noutrefresh()
        except curses.error:
            pass

    def _draw_input(self) -> None:
        h, w = self.getmaxyx()
        y = h - 1
        if y < 0:
            return
        prompt = "> "
        plen = len(prompt)
        # never write the screen's bottom-right corner cell: curses returns
        # ERR for any addstr that reaches it, and the app would silently
        # swallow the whole row
        maxtext = max(0, w - 1 - plen)
        try:
            self.stdscr.move(y, 0)
            self.stdscr.clrtoeol()
            self.stdscr.addstr(y, 0, prompt, theme.attr("input_accent", curses.A_BOLD))
            vis = self.input_buf
            if len(vis) > maxtext:
                vis = vis[len(vis) - maxtext:]
            self.stdscr.addstr(y, plen, vis, theme.attr("input"))
            self.stdscr.move(y, clamp(plen + self.input_pos, plen, w - 1))
            self.stdscr.noutrefresh()
        except curses.error:
            pass

    def _draw_modal(self) -> None:
        h, w = self.getmaxyx()
        m = self.modal
        if m is None:
            return
        if isinstance(m, PlayerActions):
            title = f" PLAYER: {m.name} "
            items = m.actions  # list of str
            mh = min(len(items) + 4, max(10, h - 6))
            mw = clamp(int(w * 0.4), 22, 44)
            tokens = [
                "kick:" + m.name,
                "ban:" + m.name,
                "ipban:" + m.name,
                "close",
            ]
            sel = m.sel
        elif isinstance(m, SettingsModal):
            title = " SERVER SETTINGS "
            items = m.actions
            mh = min(len(items) + 4, max(10, h - 6))
            mw = clamp(int(w * 0.5), 30, 60)
            sel = m.sel
        elif isinstance(m, WhitelistModal):
            title = " WHITELIST "
            items = m.actions()
            mh = min(len(m.names) + 7, max(15, h - 6))
            mw = clamp(int(w * 0.4), 28, 50)
            sel = m.sel
        elif isinstance(m, FieldModal):
            title = m.title
            mh = min(len(m.fields) + 5, max(14, h - 8))
            mw = clamp(int(w * 0.5), 34, 56)
            sel = m.sel
        else:
            title = " SELECT VERSION "
            sel = m.sel
            mh = min(26, max(14, h - 8))
            mw = clamp(int(w * 0.5), 24, 60)
        my, mx = (h - mh) // 2, (w - mw) // 2
        if self._modal_win is not None:
            try:
                self._modal_win.erase()
            except curses.error:
                pass
        win = curses.newwin(mh, mw, my, mx)
        win.bkgd(" ", theme.attr("stats"))
        win.erase()
        try:
            win.border()
        except curses.error:
            pass
        self._winline(win, 0, 2, truncate(title, mw - 4),
                      theme.attr("status_ok", curses.A_BOLD))
        if isinstance(m, PlayerActions):
            items = m.actions
            for i, label in enumerate(items):
                is_sel = i == sel
                attr = theme.attr("input_accent" if is_sel else "input",
                                  curses.A_BOLD if is_sel else 0)
                prefix = " > " if is_sel else "   "
                text = prefix + truncate(label, mw - 4)
                self._winline(win, 1 + i, 1, text, attr)
                self._hit(my + 1 + i, mx + 1, len(text) - 1, tokens[i])
        elif isinstance(m, SettingsModal):
            for i, _label in enumerate(m.actions):
                is_sel = i == sel
                attr = theme.attr("input_accent" if is_sel else "input",
                                  curses.A_BOLD if is_sel else 0)
                text, token = self._settings_row(m, i, mw)
                text = truncate(text, mw - 4)
                self._winline(win, 1 + i, 1, text, attr)
                self._hit(my + 1 + i, mx + 1, len(text) - 1, token)
        elif isinstance(m, WhitelistModal):
            names = m.names
            list_rows = max(1, mh - 5)
            for i in range(list_rows):
                if i >= len(names):
                    break
                label = names[i]
                is_sel = i == sel
                attr = theme.attr("input_accent" if is_sel else "input",
                                  curses.A_BOLD if is_sel else 0)
                prefix = " > " if is_sel else "   "
                text = prefix + truncate(label, mw - 4)
                self._winline(win, 1 + i, 1, text, attr)
                self._hit(my + 1 + i, mx + 1, len(text) - 1,
                          "whitelist-remove:" + label)
            bar = mh - 3
            x = 1
            buttons = m.actions()[len(names):]
            tokens = ["whitelist-add", "whitelist-toggle", "close"]
            for i, label in enumerate(buttons):
                bidx = len(names) + i
                is_sel = bidx == sel
                attr = theme.attr("input_accent" if is_sel else "input",
                                  curses.A_BOLD if is_sel else 0)
                btext = "[" + label + "]"
                btext = truncate(btext, mw - 2)
                self._winline(win, bar, x, btext, attr)
                self._hit(my + bar, mx + x, len(btext), tokens[i])
                x += len(btext) + 1
            if m.loading:
                self._winline(win, 1, 1, "Fetching whitelist…", theme.attr("dim"))
            elif m.error:
                self._winline(win, 1, 1, truncate("Error: " + m.error, mw - 2),
                              theme.attr("status_err"))
            if m.editing:
                self._winline(win, mh - 2, 1,
                              " add name> " + truncate(m.buf + "_", mw - 12),
                              theme.attr("input_accent", curses.A_BOLD))
            else:
                self._winline(win, mh - 2, 1,
                              truncate("Changes apply on the next server start.",
                                       mw - 2),
                              theme.attr("dim"))
        elif isinstance(m, FieldModal):
            rows = max(1, mh - 3)
            start = m.scroll_start(rows)
            for i in range(rows):
                idx = start + i
                if idx >= len(m.fields):
                    break
                is_sel = idx == sel
                attr = theme.attr("input_accent" if is_sel else "input",
                                  curses.A_BOLD if is_sel else 0)
                text = self._field_row_text(m, idx, mw)
                self._winline(win, 1 + i, 1, text, attr)
                self._hit(my + 1 + i, mx + 1, len(text) - 1, "field:" + str(idx))
            is_done = sel == len(m.fields)
            attr = theme.attr("input_accent" if is_done else "input",
                              curses.A_BOLD if is_done else 0)
            text = " > done" if is_done else "   done"
            self._winline(win, rows + 1, 1, text, attr)
            self._hit(my + rows + 1, mx + 1, len(text) - 1, "field:done")
        elif m.loading:
            self._winline(win, mh // 2, 1, "Fetching versions…", theme.attr("dim"))
        elif m.error:
            self._winline(win, mh // 2, 1, truncate("Error: " + m.error, mw - 2),
                          theme.attr("status_err"))
        else:
            self._winline(win, 1, 1, "search: " + truncate(m.query, mw - 9),
                          theme.attr("dim"))
            items = m.filtered()
            visible = mh - 5
            start = max(0, sel - visible + 1)
            if not items:
                self._winline(win, 2, 1, "no matching versions", theme.attr("dim"))
            for i in range(visible):
                idx = start + i
                if idx >= len(items):
                    break
                is_sel = idx == sel
                attr = theme.attr("input_accent" if is_sel else "input",
                                  curses.A_BOLD if is_sel else 0)
                mark = "✓" if items[idx] == m.installed else " "
                prefix = " >" if is_sel else "  "
                text = prefix + mark + " " + truncate(items[idx], mw - 5)
                self._winline(win, 2 + i, 1, text, attr)
                self._hit(my + 2 + i, mx + 1, len(text) - 1, "vinstall:" + items[idx])
        win.noutrefresh()
        self._modal_win = win

    def _draw_small(self) -> None:
        try:
            h, w = self.getmaxyx()
            self.stdscr.clear()
            self.stdscr.noutrefresh()
            self.stdscr.addstr(0, 0, " " * w, theme.attr("header"))
            self.stdscr.addstr(0, 0, truncate(f" {APP_TITLE} v{__version__}", w),
                               theme.attr("header"))
            self.stdscr.addstr(1, 0, "Terminal too small (need >= 40x10).",
                               theme.attr("status_err"))
            self.stdscr.noutrefresh()
            curses.doupdate()
        except curses.error:
            pass

    # ==================================================================
    # helpers
    # ==================================================================
    def _line_attr(self, text: str) -> int:
        low = text.lower()
        if "stacktrace" in low or "error" in low:
            return theme.attr("status_err")
        if "warn" in low:
            return theme.attr("status_warn")
        if "done" in low and "(" in text:
            return theme.attr("status_ok")
        if "<" in text and ">" in text:
            return theme.attr("console_accent")
        return theme.attr("console")

    def _winline(self, win, y: int, x: int, string: str, attr: int) -> None:
        try:
            win.addstr(y, x, string, attr)
        except curses.error:
            pass

    def shutdown(self) -> None:
        try:
            self.server.shutdown()
        except Exception:
            pass