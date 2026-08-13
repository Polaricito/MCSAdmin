"""Unit tests for MCSAdmin (no network required)."""

import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcsadmin.config import Config, default_config_dir, generate_password
from mcsadmin import iconutil
from mcsadmin.rcon import parse_player_list
from mcsadmin.stats import SystemStats
from mcsadmin.util import LogBuffer, clamp, fmt_bytes, fmt_seconds, truncate
from mcsadmin import javavm
from mcsadmin import versions


class TestUtil(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(clamp(5, 0, 10), 5)
        self.assertEqual(clamp(-3, 0, 10), 0)
        self.assertEqual(clamp(99, 0, 10), 10)

    def test_fmt_bytes(self):
        self.assertEqual(fmt_bytes(1024), "1 KiB")
        self.assertEqual(fmt_bytes(1024 * 1024 * 1024), "1 GiB")
        self.assertEqual(fmt_bytes(1536), "1.5 KiB")
        self.assertEqual(fmt_bytes(0), "0 B")

    def test_fmt_seconds(self):
        self.assertEqual(fmt_seconds(90), "01:30")
        self.assertEqual(fmt_seconds(3600), "01:00:00")

    def test_truncate(self):
        self.assertEqual(truncate("hello", 10), "hello")
        self.assertEqual(len(truncate("hello world", 5)), 5)


class TestLogBuffer(unittest.TestCase):
    def test_bounded(self):
        buf = LogBuffer(maxlen=3)
        for i in range(10):
            buf.append(f"line{i}")
        self.assertEqual(buf.tail(10), ["line7", "line8", "line9"])


class TestRCONParse(unittest.TestCase):
    def test_list_with_players(self):
        raw = (
            "[09:00:00] [Server thread/INFO]: There are 3 of a max of 20 players"
            " online: Alice, Bob, Notch"
        )
        self.assertEqual(parse_player_list(raw), ["Alice", "Bob", "Notch"])

    def test_list_empty(self):
        raw = "[09:00:00] [Server thread/INFO]: There are 0 of a max of 20 players online: "
        self.assertEqual(parse_player_list(raw), [])


class TestPlayerCap(unittest.TestCase):
    def test_max_players_regex(self):
        from mcsadmin.server import MAX_RE

        raw = "[09:00:00] [Server thread/INFO]: There are 10 of a max of 20 players online: A, B"
        m = MAX_RE.search(raw)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), 20)


class TestJavaFlags(unittest.TestCase):
    def test_netty_flag_removed(self):
        from mcsadmin.config import Config
        from mcsadmin.server import ServerManager
        from mcsadmin.util import LogBuffer

        with tempfile.TemporaryDirectory() as d:
            mgr = ServerManager(Config(os.path.join(d, "c.json")), LogBuffer())
            flags = " ".join(mgr._java_flags())
            self.assertNotIn("-Dio.netty.transport.noNative=true", flags)
            self.assertTrue(flags.endswith("server.jar nogui"))

    def test_cores_flag_when_configured(self):
        from mcsadmin.config import Config
        from mcsadmin.server import ServerManager
        from mcsadmin.util import LogBuffer

        with tempfile.TemporaryDirectory() as d:
            cfg = Config(os.path.join(d, "c.json"))
            cfg.java["cores"] = 4
            mgr = ServerManager(cfg, LogBuffer())
            flags = " ".join(mgr._java_flags())
            self.assertIn("-XX:ActiveProcessorCount=4", flags)

    def test_cores_flag_absent_when_unset(self):
        from mcsadmin.config import Config
        from mcsadmin.server import ServerManager
        from mcsadmin.util import LogBuffer

        with tempfile.TemporaryDirectory() as d:
            mgr = ServerManager(Config(os.path.join(d, "c.json")), LogBuffer())
            flags = " ".join(mgr._java_flags())
            self.assertNotIn("ActiveProcessorCount", flags)

    def test_native_access_flag_on_jdk17_plus(self):
        from mcsadmin import javavm
        from mcsadmin.config import Config
        from mcsadmin.server import ServerManager
        from mcsadmin.util import LogBuffer

        with tempfile.TemporaryDirectory() as d:
            mgr = ServerManager(Config(os.path.join(d, "c.json")), LogBuffer())
            with unittest.mock.patch.object(
                javavm, "installed_java_major", return_value=21
            ):
                flags = mgr._java_flags("/usr/bin/java")
            self.assertIn("--enable-native-access=ALL-UNNAMED", flags)

    def test_native_access_flag_absent_on_old_java(self):
        # Java 8/11 don't understand the option; adding it would abort
        # startup for the very old versions this tool supports (e.g. 1.8.9).
        from mcsadmin import javavm
        from mcsadmin.config import Config
        from mcsadmin.server import ServerManager
        from mcsadmin.util import LogBuffer

        with tempfile.TemporaryDirectory() as d:
            mgr = ServerManager(Config(os.path.join(d, "c.json")), LogBuffer())
            with unittest.mock.patch.object(
                javavm, "installed_java_major", return_value=8
            ):
                flags = mgr._java_flags("/usr/bin/java")
            self.assertNotIn("--enable-native-access", flags)


class TestNativeTransportProperty(unittest.TestCase):
    def _props(self, mgr, path):
        from mcsadmin.server import read_properties

        mgr._write_properties(path)
        return read_properties(path)

    def test_forced_false_when_absent(self):
        from mcsadmin.config import Config
        from mcsadmin.server import ServerManager
        from mcsadmin.util import LogBuffer

        with tempfile.TemporaryDirectory() as d:
            cfg = Config(os.path.join(d, "c.json"))
            mgr = ServerManager(cfg, LogBuffer())
            path = os.path.join(d, "server.properties")
            self.assertEqual(self._props(mgr, path)["use-native-transport"], "false")

    def test_forced_false_when_true(self):
        from mcsadmin.config import Config
        from mcsadmin.server import ServerManager
        from mcsadmin.util import LogBuffer

        with tempfile.TemporaryDirectory() as d:
            cfg = Config(os.path.join(d, "c.json"))
            mgr = ServerManager(cfg, LogBuffer())
            path = os.path.join(d, "server.properties")
            with open(path, "w") as fh:
                fh.write("use-native-transport=true\n")
            self.assertEqual(self._props(mgr, path)["use-native-transport"], "false")

    def test_left_alone_when_already_false(self):
        from mcsadmin.config import Config
        from mcsadmin.server import ServerManager
        from mcsadmin.util import LogBuffer

        with tempfile.TemporaryDirectory() as d:
            cfg = Config(os.path.join(d, "c.json"))
            mgr = ServerManager(cfg, LogBuffer())
            path = os.path.join(d, "server.properties")
            with open(path, "w") as fh:
                fh.write("use-native-transport=false\n")
            self.assertEqual(self._props(mgr, path)["use-native-transport"], "false")

    def test_world_options_written(self):
        from mcsadmin.config import Config
        from mcsadmin.server import ServerManager
        from mcsadmin.util import LogBuffer

        with tempfile.TemporaryDirectory() as d:
            cfg = Config(os.path.join(d, "c.json"))
            cfg.set("world", {"difficulty": "hard", "pvp": "false"})
            mgr = ServerManager(cfg, LogBuffer())
            path = os.path.join(d, "server.properties")
            props = self._props(mgr, path)
            self.assertEqual(props.get("difficulty"), "hard")
            self.assertEqual(props.get("pvp"), "false")


class TestFieldModal(unittest.TestCase):
    """World Options / JVM modal editing logic (no curses needed)."""

    def _app(self, tmpdir):
        from mcsadmin.tui import App
        from mcsadmin.util import LogBuffer

        app = App.__new__(App)
        app.config = Config(os.path.join(tmpdir, "c.json"))
        app.log = LogBuffer()
        app.message = ""
        app._message_at = 0.0
        app.modal = None
        app.prev_modal = None
        app.server = type(
            "S",
            (),
            {"proc": None, "pid": None, "players": set(), "player_ips": {}},
        )()
        return app

    def test_save_world_options_serializes_and_drops_defaults(self):
        from mcsadmin.tui import FieldModal, WORLD_FIELDS

        with tempfile.TemporaryDirectory() as d:
            app = self._app(d)
            m = FieldModal("t", WORLD_FIELDS,
                           {"difficulty": "hard", "pvp": False,
                            "view-distance": None}, None)
            app._save_world_options(m.values)
            self.assertEqual(app.config.get("world"),
                             {"difficulty": "hard", "pvp": "false"})

    def test_world_options_persist_through_setup_files(self):
        # full path: open World Options from settings, edit, hit done, and
        # verify config + server.properties both reflect the new values
        from mcsadmin.server import ServerManager, read_properties
        from mcsadmin.tui import SettingsModal, WORLD_FIELDS
        from mcsadmin.util import LogBuffer

        with tempfile.TemporaryDirectory() as d:
            app = self._app(d)
            app.config.set("server_dir", d)
            app.server = ServerManager(app.config, LogBuffer())
            app.modal = SettingsModal(app.config, d)
            app._open_world_modal()
            m = app.modal
            m.values["difficulty"] = "hard"
            m.values["max-players"] = 20
            m.sel = len(WORLD_FIELDS)
            app._handle_modal_key(10)  # done -> save + back to settings
            self.assertIsInstance(app.modal, SettingsModal)
            props = read_properties(os.path.join(d, "server.properties"))
            self.assertEqual(props.get("difficulty"), "hard")
            self.assertEqual(props.get("max-players"), "20")

    def test_open_world_modal_seeds_from_properties(self):
        with tempfile.TemporaryDirectory() as d:
            app = self._app(d)
            app.config.set("server_dir", d)
            prop = os.path.join(d, "server.properties")
            with open(prop, "w") as fh:
                fh.write("difficulty=normal\nview-distance=12\npvp=false\n")
            app.config.set("world", {"gamemode": "spectator"})
            app._open_world_modal()
            m = app.modal
            self.assertEqual(m.values["difficulty"], "normal")
            self.assertEqual(m.values["view-distance"], 12)
            self.assertIs(m.values["pvp"], False)
            self.assertEqual(m.values["gamemode"], "spectator")
            self.assertIsNone(m.values["hardcore"])

    def test_enter_on_done_saves_and_closes(self):
        from mcsadmin.tui import SettingsModal, WORLD_FIELDS

        with tempfile.TemporaryDirectory() as d:
            app = self._app(d)
            app.config.set("server_dir", d)
            app.config.set("server_icon", None)
            app.modal = SettingsModal(app.config, d)
            app._open_world_modal()  # sets prev_modal = settings
            m = app.modal
            m.values["difficulty"] = "easy"
            m.sel = len(WORLD_FIELDS)
            app.modal = m
            app._handle_modal_key(10)  # Enter on "done"
            # returns to the settings screen (sub-menu pops back)
            self.assertIsInstance(app.modal, SettingsModal)
            self.assertEqual(app.config.get("world")["difficulty"], "easy")

    def test_esc_from_submenu_returns_to_settings(self):
        from mcsadmin.tui import FieldModal, SettingsModal

        with tempfile.TemporaryDirectory() as d:
            app = self._app(d)
            app.config.set("server_dir", d)
            app.modal = SettingsModal(app.config, d)
            app._open_java_modal()
            self.assertIsInstance(app.modal, FieldModal)
            app._handle_modal_key(27)  # ESC
            self.assertIsInstance(app.modal, SettingsModal)

    def test_save_java_settings_uses_ram_as_max(self):
        with tempfile.TemporaryDirectory() as d:
            app = self._app(d)
            app._save_java_settings({"ram_mb": 4096, "cores": 8})
            # the assigned RAM becomes the max heap, with min pinned to it
            self.assertEqual(app.config.java["max_memory_mb"], 4096)
            self.assertEqual(app.config.java["min_memory_mb"], 4096)
            self.assertEqual(app.config.java["cores"], 8)

    def test_cycle_bool_and_choice(self):
        from mcsadmin.tui import FieldModal, WORLD_FIELDS

        with tempfile.TemporaryDirectory() as d:
            app = self._app(d)
            f = FieldModal("t", WORLD_FIELDS,
                           {"pvp": None, "difficulty": None}, None)
            di = next(i for i, e in enumerate(WORLD_FIELDS) if e[0] == "difficulty")
            app._field_enter(f, di)
            self.assertEqual(f.values["difficulty"], "peaceful")
            app._field_enter(f, di)
            self.assertEqual(f.values["difficulty"], "easy")
            pi = next(i for i, e in enumerate(WORLD_FIELDS) if e[0] == "pvp")
            app._field_enter(f, pi)
            self.assertIs(f.values["pvp"], True)
            app._field_enter(f, pi)
            self.assertIs(f.values["pvp"], False)

    def test_int_editing_commits_and_blank_is_auto(self):
        from mcsadmin.tui import FieldModal, JVM_FIELDS

        with tempfile.TemporaryDirectory() as d:
            app = self._app(d)
            f = FieldModal("t", JVM_FIELDS, {"cores": None}, None)
            ji = next(i for i, e in enumerate(JVM_FIELDS) if e[0] == "cores")
            f.sel = ji
            app._field_enter(f, ji)
            for ch_ in "42":
                app._handle_field_key(f, ord(ch_))
            self.assertEqual(f.edit_buf, "42")
            app._commit_field(f)
            self.assertEqual(f.values["cores"], 42)
            self.assertIsNone(f.editing)
            # empty buffer -> None (auto)
            app._field_enter(f, ji)
            for _ in range(len(f.edit_buf)):
                app._handle_field_key(f, 263)  # backspace all digits
            app._commit_field(f)
            self.assertIsNone(f.values["cores"])

    def test_settings_row_tokens(self):
        from mcsadmin.tui import SettingsModal

        with tempfile.TemporaryDirectory() as d:
            app = self._app(d)
            m = SettingsModal(app.config, d)
            m.sel = 2
            text, token = app._settings_row(m, 2, 40)
            self.assertEqual(token, "settings:java")
            self.assertIn("jvm:", text)
            m.sel = 3
            text, token = app._settings_row(m, 3, 40)
            self.assertEqual(token, "settings:done")
            self.assertIn("save", text)
            # world options has its own key now, not part of server settings
            self.assertNotIn("world options", m.actions)

    def test_w_hotkey_opens_world_options(self):
        from mcsadmin.tui import FieldModal

        with tempfile.TemporaryDirectory() as d:
            app = self._app(d)
            app.config.set("server_dir", d)
            app._run_hotkey(ord("W"))
            self.assertIsInstance(app.modal, FieldModal)
            self.assertIn("WORLD OPTIONS", app.modal.title)

    def test_server_ram_reflects_live_usage(self):
        from mcsadmin.tui import App

        app = App.__new__(App)
        app.config = Config(os.path.join(tempfile.mkdtemp(), "c.json"))
        app.config.java["max_memory_mb"] = 2048
        app.sys_mem = {"total": 16 * 1024**3, "available": None}
        app.sys_cpu = None
        app.srv_cpu = None
        app.srv_mem = 400 * 1024 * 1024
        app.ext_pid = None
        app.server = type("S", (), {"pid": 1234, "rcon_ready": False})()
        rows = app._stats_rows()
        ram = [r for r in rows if r[0] == "Server RAM"][0]
        # usage is shown against the assigned heap, not the machine's RAM
        self.assertEqual(ram[1], "400 MiB / 2 GiB")
        self.assertGreater(ram[2], 0)
        self.assertNotIn("16 GiB", ram[1])

    def test_no_rcon_row_in_resources(self):
        from mcsadmin.tui import App

        app = App.__new__(App)
        app.config = Config(os.path.join(tempfile.mkdtemp(), "c.json"))
        app.sys_mem = {"total": 16 * 1024**3, "available": 8 * 1024**3}
        app.sys_cpu = None
        app.srv_cpu = None
        app.srv_mem = 400 * 1024 * 1024
        app.ext_pid = 99
        app.server = type("S", (), {"pid": 1234, "rcon_ready": True})()
        labels = [r[0] for r in app._stats_rows()]
        self.assertNotIn("rcon", labels)

    def test_footer_buttons_include_world_and_no_sync(self):
        from mcsadmin.tui import App

        app = App.__new__(App)
        app.server = type(
            "S", (), {"proc": type("P", (), {"poll": lambda s: None})()}
        )()
        app._server_running = lambda: True
        running = app._footer_buttons()
        tokens = [t for _l, t in running]
        # while running, world options must be hidden from the bottom bar
        self.assertNotIn("world", tokens)
        self.assertNotIn("sync", tokens)
        self.assertFalse(any("[W] world" in label for label, _t in running))
        app._server_running = lambda: False
        stopped = app._footer_buttons()
        tokens = [t for _l, t in stopped]
        self.assertIn("world", tokens)
        self.assertNotIn("sync", tokens)
        self.assertTrue(any("[W] world" in label for label, _t in stopped))

    def test_settings_hotkey_blocked_while_running(self):
        from mcsadmin.tui import App

        app = App.__new__(App)
        app.config = Config(os.path.join(tempfile.mkdtemp(), "c.json"))
        app.config.set("server_dir", tempfile.mkdtemp())
        app.message = ""
        app._message_at = 0.0
        app.log = __import__("mcsadmin.util", fromlist=["LogBuffer"]).LogBuffer()
        app.modal = None
        app.prev_modal = None
        app._server_running = lambda: True
        app._run_hotkey(ord("E"))
        self.assertIsNone(app.modal)
        self.assertTrue(app.message)
        app._run_hotkey(ord("W"))
        self.assertIsNone(app.modal)

    def test_player_join_log_captures_ip(self):
        from mcsadmin.server import ServerManager

        with tempfile.TemporaryDirectory() as d:
            s = ServerManager(Config(os.path.join(d, "c.json")), __import__(
                "mcsadmin.util", fromlist=["LogBuffer"]).LogBuffer())
            s._detect_player_events(
                "[Server thread/INFO]: Steve[/192.168.1.5:54321] "
                "logged in with entity id 42 at (0.5, 64.0, 0.5)"
            )
            self.assertIn("Steve", s.players)
            self.assertEqual(s.player_ips["Steve"], "192.168.1.5")
            s._detect_player_events("Steve left the game")
            self.assertNotIn("Steve", s.players)
            self.assertNotIn("Steve", s.player_ips)

    def test_player_actions_include_ip_ban_no_whitelist(self):
        from mcsadmin.tui import PlayerActions

        m = PlayerActions("Steve")
        self.assertIn("IP ban", m.actions)
        # the whitelist editor lives in world options, not the player menu
        self.assertNotIn("Whitelist", m.actions)

    def test_world_options_has_online_mode_and_whitelist(self):
        from mcsadmin.tui import WORLD_FIELDS

        keys = [k for k, _l, _t in WORLD_FIELDS]
        self.assertIn("online-mode", keys)
        self.assertIn("whitelist", keys)

    def test_whitelist_row_active_marker(self):
        from mcsadmin.tui import App, FieldModal, WORLD_FIELDS

        app = App.__new__(App)
        idx = [k for k, _l, _t in WORLD_FIELDS].index("whitelist")
        active = FieldModal(" WORLD OPTIONS ", WORLD_FIELDS,
                            {"white-list": True}, lambda v: None)
        text = app._field_row_text(active, idx, 40)
        self.assertIn("whitelist >", text)
        inactive = FieldModal(" WORLD OPTIONS ", WORLD_FIELDS,
                              {"white-list": False}, lambda v: None)
        text = app._field_row_text(inactive, idx, 40)
        self.assertIn("whitelist", text)
        self.assertNotIn("whitelist >", text)

    def test_right_arrow_opens_whitelist_modal(self):
        from mcsadmin.tui import FieldModal, WhitelistModal, WORLD_FIELDS

        key_right, key_esc = 261, 27  # not defined until curses.setupterm()
        with tempfile.TemporaryDirectory() as d:
            app = self._app(d)
            app.server = type(
                "S",
                (),
                {"players": {"Steve"}, "player_ips": {},
                 "rcon_command": lambda self, cmd: "There are 1 whitelisted players: Alex"},
            )()
            m = FieldModal(" WORLD OPTIONS ", WORLD_FIELDS, {}, lambda v: None)
            m.sel = [k for k, _l, _t in WORLD_FIELDS].index("whitelist")
            app.modal = m
            app._handle_field_key(m, key_right)
            self.assertIsInstance(app.modal, WhitelistModal)
            self.assertIs(app.prev_modal, m)
            # ESC returns to world options
            app._handle_modal_key(key_esc)
            self.assertIs(app.modal, m)

    def test_whitelist_parse(self):
        from mcsadmin.server import parse_whitelist

        self.assertEqual(
            parse_whitelist("There are 2 whitelisted players: Alex, Steve"),
            ["Alex", "Steve"],
        )
        self.assertEqual(parse_whitelist("There are 0 whitelisted players"), [])
        self.assertEqual(parse_whitelist(""), [])

    def test_whitelist_add_remove_send_commands(self):
        from mcsadmin.tui import WhitelistModal

        with tempfile.TemporaryDirectory() as d:
            app = self._app(d)
            sent = []
            app.server = type(
                "S",
                (),
                {"proc": type("P", (), {"poll": lambda self: None})(),
                 "rcon_command": lambda self, cmd: "There are 0 whitelisted players",
                 "send_command": lambda self, cmd: sent.append(cmd)},
            )()
            m = WhitelistModal(app._whitelist_query)
            m._load()
            m.buf = "Alex"
            app._whitelist_add(m)
            self.assertIn("whitelist add Alex", sent)
            app._whitelist_remove(m, "Alex")
            self.assertIn("whitelist remove Alex", sent)

    def test_whitelist_toggle_flips_file_and_config(self):
        from mcsadmin.server import read_properties, set_property

        with tempfile.TemporaryDirectory() as d:
            app = self._app(d)
            app.config.set("server_dir", d)
            props = os.path.join(d, "server.properties")
            from mcsadmin.tui import WhitelistModal

            set_property(props, "white-list", "true")
            m = WhitelistModal(lambda: [], enabled=True)
            app._whitelist_toggle(m)  # on  -> off
            self.assertEqual(read_properties(props)["white-list"], "false")
            self.assertEqual(m.enabled, False)
            app._whitelist_toggle(m)  # off -> on
            self.assertEqual(read_properties(props)["white-list"], "true")
            self.assertEqual(m.enabled, True)
            self.assertEqual(
                app.config.get("world", {}).get("white-list"), "true"
            )

    def test_whitelist_actions_label_follows_state(self):
        from mcsadmin.tui import WhitelistModal

        self.assertEqual(
            WhitelistModal(lambda: [], enabled=True).toggle_label(),
            "Disable whitelist",
        )
        self.assertEqual(
            WhitelistModal(lambda: [], enabled=False).toggle_label(),
            "Enable whitelist",
        )

    def test_ipban_uses_player_connection_ip(self):
        from mcsadmin.tui import PlayerActions

        with tempfile.TemporaryDirectory() as d:
            app = self._app(d)
            sent = []
            app.server = type(
                "S",
                (),
                {"player_ips": {"Steve": "192.168.1.5"},
                 "send_command": lambda self, cmd: sent.append(cmd)},
            )()
            m = PlayerActions("Steve")
            app._run_player_action(m, "IP ban")
            self.assertIn("ban-ip 192.168.1.5", sent)
            # unknown IP -> no command, just a notification
            app.server = type(
                "S",
                (),
                {"player_ips": {}, "send_command": lambda self, cmd: sent.append(cmd)},
            )()
            app._run_player_action(PlayerActions("Ghost"), "IP ban")
            self.assertEqual(len(sent), 1)

    def test_bar_only_shows_fill(self):
        from mcsadmin.tui import App

        app = App.__new__(App)
        bar = app._bar_only(0.5, 30)
        self.assertIn("[", bar)
        self.assertIn("#", bar)
        self.assertIn("-", bar)

    def test_draw_stats_three_line_layout(self):
        # bar on top, name below, value under it, for a measurable row
        from mcsadmin.tui import App

        class FakeWin:
            def __init__(self, h, w):
                self.h, self.w = h, w
                self.rows = [""] * h
                self.bordered = False

            def getmaxyx(self):
                return self.h, self.w

            def erase(self):
                pass

            def border(self):
                self.bordered = True

            def addstr(self, y, x, s, _attr=0):
                if 0 <= y < self.h:
                    line = self.rows[y]
                    pad = x - len(line)
                    if pad > 0:
                        line += " " * pad
                    self.rows[y] = line[:x] + s

            def noutrefresh(self):
                pass

        app = App.__new__(App)
        app.sys_mem = {"total": 4 * 1024**3, "available": 3 * 1024**3}
        app.sys_cpu = 25.0
        app.srv_cpu = 50.0
        app.srv_mem = 900 * 1024 * 1024
        app.ext_pid = 123
        app.server = type("S", (), {"pid": 456, "started_at": None,
                                    "status": "running", "rcon_ready": False})()
        app._panes = {}
        win = FakeWin(24, 40)
        app._panes["stats"] = win
        app._draw_stats()
        lines = [l for l in win.rows if l.strip()]
        joined = "\n".join(lines)
        self.assertIn("Server RAM", joined)
        self.assertIn("System RAM", joined)
        # Server RAM is shown against the assigned heap (default 2048 MiB = 2 GiB)
        self.assertIn("900 MiB / 2 GiB", joined)
        self.assertIn("1 GiB / 4 GiB", joined)
        self.assertTrue(any(l.strip().startswith("[") and "#" in l and "-" in l
                            for l in lines))

    def test_max_players_loaded_from_properties_default_20(self):
        from mcsadmin.server import ServerManager
        from mcsadmin.util import LogBuffer

        with tempfile.TemporaryDirectory() as d:
            cfg = Config(os.path.join(d, "c.json"))
            cfg.set("server_dir", os.path.join(d, "srv"))
            s = ServerManager(cfg, LogBuffer())
            s.setup_files()
            self.assertEqual(s.max_players, 20)
            # a custom max-players is honored
            props = os.path.join(d, "srv", "server.properties")
            with open(props, "a") as fh:
                fh.write("max-players=15\n")
            s._load_max_players()
            self.assertEqual(s.max_players, 15)

    def test_icon_persists_when_untouched(self):
        # the configured icon lives outside the discovered candidates; saving
        # without touching the icon field must keep it, not revert to none
        from mcsadmin.server import ServerManager
        from mcsadmin.tui import SettingsModal
        from mcsadmin.util import LogBuffer

        with tempfile.TemporaryDirectory() as d:
            pixels = [(x % 256, (x * 2) % 256, 255, 255)
                      for x in range(64 * 64)]
            src = os.path.join(d, "custom-icon.png")
            with open(src, "wb") as fh:
                fh.write(iconutil._encode_png(64, pixels))
            app = self._app(d)
            app.config.set("server_dir", os.path.join(d, "srv"))
            app.config.set("server_icon", src)
            app.server = ServerManager(app.config, LogBuffer())
            m = SettingsModal(app.config, os.path.join(d, "srv"))
            self.assertEqual(m.icon_i, -1)  # not in the discovered candidates
            app.modal = m
            app._apply_settings(m)
            self.assertEqual(app.config.get("server_icon"), src)
            self.assertTrue(
                os.path.exists(os.path.join(d, "srv", "server-icon.png"))
            )


class TestStatsBar(unittest.TestCase):
    def test_half_fill_bar(self):
        from mcsadmin.tui import App

        app = App.__new__(App)
        line = app._stats_line("players", "10/20", 0.5, 30)
        self.assertIn("10/20", line)
        self.assertIn("#", line)
        self.assertIn("-", line)

    def test_plain_row_when_no_fraction(self):
        from mcsadmin.tui import App

        app = App.__new__(App)
        line = app._stats_line("rcon", "ready", None, 30)
        self.assertEqual(line, "rcon: ready")

    def test_bar_line_shape(self):
        from mcsadmin.tui import App

        app = App.__new__(App)
        line = app._bar_line("players", 0.5, 30)
        self.assertIn("players [", line)
        self.assertTrue(line.strip().endswith("]"))

    def test_centered_is_centered(self):
        from mcsadmin.tui import App

        line = App._centered("14%", 30)
        self.assertTrue(line.startswith(" "))
        self.assertEqual(len(line.strip()), 3)
        # symmetric padding
        left = len(line) - len(line.lstrip())
        self.assertTrue(abs(left - (28 - 3 - left)) <= 1)


class TestConfig(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "conf.json")
            cfg = Config(path)
            cfg.set("version", "1.21.1")
            cfg2 = Config(path)
            self.assertEqual(cfg2.get("version"), "1.21.1")

    def test_password(self):
        self.assertTrue(generate_password())
        self.assertEqual(len(generate_password(8)), 8)

    def test_nested_defaults_are_not_shared(self):
        # regression: shallow DEFAULTS.copy() shared the java_flags dict, so
        # saving RAM/cores in one Config instance leaked into every other one.
        with tempfile.TemporaryDirectory() as d:
            c1 = Config(os.path.join(d, "a.json"))
            c2 = Config(os.path.join(d, "b.json"))
            c1.java["cores"] = 8
            self.assertIsNone(c2.java.get("cores"))

    def test_data_dir_never_relative(self):
        # the "writes into /usr/bin or /usr/share" bug guard: a missing HOME
        # must never produce a path relative to the install/cwd directory.
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            d = default_config_dir()
            self.assertTrue(os.path.isabs(d))
            self.assertNotIn("~", d)

    def test_data_dir_override_env(self):
        with unittest.mock.patch.dict(
            os.environ, {"MCSADMIN_DATA_DIR": "/tmp/mcs-data"}, clear=True
        ):
            self.assertEqual(default_config_dir(), "/tmp/mcs-data")


class TestIconUtil(unittest.TestCase):
    def _make_png(self, size: int) -> bytes:
        pixels = []
        for y in range(size):
            for x in range(size):
                key = 8 * (x * size + y)
                pixels.append((key % 256, (key * 2) % 256, (key * 3) % 256, 255))
        return iconutil._encode_png(size, pixels)

    def test_normalize_resizes_to_64(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "big.png")
            dst = os.path.join(d, "server-icon.png")
            with open(src, "wb") as fh:
                fh.write(self._make_png(128))
            err = iconutil.normalize_icon(src, dst)
            self.assertIsNone(err)
            with open(dst, "rb") as fh:
                out = fh.read()
            w, h, color, _, _ = iconutil._decode_png(out)
            self.assertEqual((w, h), (iconutil.ICON_SIZE, iconutil.ICON_SIZE))
            self.assertEqual(color, 6)

    def test_normalize_rejects_garbage(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "junk.png")
            with open(src, "wb") as fh:
                fh.write(b"not a png at all")
            err = iconutil.normalize_icon(src, os.path.join(d, "out.png"))
            self.assertIsNotNone(err)


class TestJavavm(unittest.TestCase):
    def test_classfile_mapping(self):
        # major 52 -> java 8, major 65 -> java 21
        self.assertEqual(javavm._class_to_feature(52), 8)
        self.assertEqual(javavm._class_to_feature(65), 21)
        self.assertEqual(javavm._class_to_feature(69), 25)


class TestInstalledVersionMarker(unittest.TestCase):
    def _manifest(self):
        info = [versions.VersionInfo(v, "release", f"https://x/{v}.json")
                for v in ("1.21.11", "1.21.1", "1.20.4")]
        return versions.Manifest("1.21.11", "1.21.11", info)

    def _install(self, server_dir, selection):
        with unittest.mock.patch.object(
            versions, "fetch_manifest", return_value=self._manifest()
        ):
            with unittest.mock.patch.object(
                versions, "download_file"
            ) as dl, unittest.mock.patch.object(
                versions, "server_jar_url", return_value="https://x/server.jar"
            ):
                v_id, jar = versions.install_server_jar(server_dir, selection)
        return v_id, jar, dl

    def test_writes_marker_after_download(self):
        with tempfile.TemporaryDirectory() as d:
            v_id, jar, dl = self._install(d, "1.21.11")
            self.assertEqual(v_id, "1.21.11")
            dl.assert_called_once()
            self.assertEqual(versions.read_installed_version(d), "1.21.11")
            self.assertTrue(os.path.isdir(d))

    def test_skips_download_when_marker_matches(self):
        with tempfile.TemporaryDirectory() as d:
            versions.write_installed_version(d, "1.21.11")
            with open(os.path.join(d, "server.jar"), "w") as fh:
                fh.write("x")
            v_id, _jar, dl = self._install(d, "1.21.11")
            self.assertEqual(v_id, "1.21.11")
            dl.assert_not_called()

    def test_redownloads_when_switching_version(self):
        # switching away from the recorded version must replace server.jar,
        # even though a jar already exists (the old 'stuck on latest' bug)
        with tempfile.TemporaryDirectory() as d:
            versions.write_installed_version(d, "1.21.1")
            with open(os.path.join(d, "server.jar"), "w") as fh:
                fh.write("stale")
            v_id, _jar, dl = self._install(d, "1.20.4")
            self.assertEqual(v_id, "1.20.4")
            dl.assert_called_once()
            self.assertEqual(versions.read_installed_version(d), "1.20.4")

    def test_latest_replaces_different_installed(self):
        # pressing "install latest" against an older installed version must
        # re-download, update the marker/checkmark and rotate worlds
        with tempfile.TemporaryDirectory() as d:
            versions.write_installed_version(d, "1.21.1")
            with open(os.path.join(d, "server.jar"), "w") as fh:
                fh.write("x")
            v_id, _jar, dl = self._install(d, None)
            self.assertEqual(v_id, "1.21.11")
            dl.assert_called_once()
            self.assertEqual(versions.read_installed_version(d), "1.21.11")

    def test_latest_keeps_when_already_latest(self):
        with tempfile.TemporaryDirectory() as d:
            versions.write_installed_version(d, "1.21.11")
            with open(os.path.join(d, "server.jar"), "w") as fh:
                fh.write("x")
            v_id, _jar, dl = self._install(d, "latest")
            self.assertEqual(v_id, "1.21.11")
            dl.assert_not_called()


class TestWorldRotation(unittest.TestCase):
    def test_switch_rotates_folders(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "server.properties"), "w") as fh:
                fh.write("level-name=world\n")
            os.makedirs(os.path.join(d, "world"))
            with open(os.path.join(d, "world", "old.dat"), "w") as fh: fh.write("a")
            os.makedirs(os.path.join(d, "1.8.9world"))
            with open(os.path.join(d, "1.8.9world", "new.dat"), "w") as fh: fh.write("b")

            versions.reorg_worlds(d, "1.16.5", "1.8.9")

            self.assertTrue(os.path.isdir(os.path.join(d, "1.16.5world")))
            self.assertFalse(os.path.exists(os.path.join(d, "1.8.9world")))
            self.assertTrue(os.path.isdir(os.path.join(d, "world")))
            self.assertTrue(
                os.path.exists(os.path.join(d, "1.16.5world", "old.dat"))
            )
            self.assertTrue(os.path.exists(os.path.join(d, "world", "new.dat")))

    def test_roundtrip_restores_world(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "server.properties"), "w") as fh:
                fh.write("level-name=mysave\n")
            os.makedirs(os.path.join(d, "mysave"))
            with open(os.path.join(d, "mysave", "v16.dat"), "w") as fh: fh.write("x")

            versions.reorg_worlds(d, "1.16.5", "1.8.9")
            versions.reorg_worlds(d, "1.8.9", "1.16.5")

            self.assertTrue(os.path.isdir(os.path.join(d, "mysave")))
            self.assertTrue(
                os.path.exists(os.path.join(d, "mysave", "v16.dat"))
            )
            self.assertFalse(os.path.exists(os.path.join(d, "1.8.9mysave")))

    def test_noop_when_same_or_empty(self):
        with tempfile.TemporaryDirectory() as d:
            versions.reorg_worlds(d, "", "1.8.9")  # no old version
            versions.reorg_worlds(d, "1.8.9", "1.8.9")  # identical

    def test_latest_fallback_prevents_data_loss(self):
        # recreate the reported bug: switch 26.1.1 -> 1.8.9 (world stashed),
        # then back to "latest" which now resolves to 26.2 with no stash of
        # its own. The previously-latest world must be restored, not a wiped
        # one.
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "server.properties"), "w") as fh:
                fh.write("level-name=world\n")
            os.makedirs(os.path.join(d, "world"))
            with open(os.path.join(d, "world", "v26.dat"), "w") as fh:
                fh.write("mine")
            # "install latest" (first time) recorded which build is the latest
            versions.reorg_worlds(d, None, "26.1.1", "latest")

            versions.reorg_worlds(d, "26.1.1", "1.8.9")
            # user plays 1.8.9, generating a fresh world
            os.makedirs(os.path.join(d, "world"))
            with open(os.path.join(d, "world", "v189.dat"), "w") as fh:
                fh.write("old")

            versions.reorg_worlds(d, "1.8.9", "26.2", "latest")

            # the 26.1.1 world came back (not a fresh, empty one)
            self.assertTrue(
                os.path.exists(os.path.join(d, "world", "v26.dat"))
            )
            self.assertTrue(os.path.isdir(os.path.join(d, "1.8.9world")))
            self.assertFalse(os.path.exists(os.path.join(d, "26.1.1world")))


class TestStats(unittest.TestCase):
    def test_system_mem_shape(self):
        m = SystemStats.system_mem()
        self.assertIn("total", m)
        self.assertIn("available", m)

    def test_process_cpu_returns_value_after_baseline(self):
        import subprocess
        import time

        if not os.path.exists("/proc"):
            self.skipTest("procfs only")
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; t=time.time()\nwhile time.time()-t<3: pass"]
        )
        try:
            s = SystemStats()
            self.assertIsNone(s.process_cpu(p.pid))  # baseline sample
            time.sleep(1.0)
            val = s.process_cpu(p.pid)
            self.assertIsNotNone(val)
            self.assertGreaterEqual(val, 0.0)
            self.assertLessEqual(val, 100.0)
        finally:
            p.kill()


class TestSetProperty(unittest.TestCase):
    def _path(self, d):
        p = os.path.join(d, "server.properties")
        with open(p, "w") as fh:
            fh.write("# comment\ngamemode=survival\nwhite-list=true\n")
        return p

    def test_updates_existing_key_in_place(self):
        from mcsadmin.server import read_properties, set_property

        with tempfile.TemporaryDirectory() as d:
            p = self._path(d)
            set_property(p, "white-list", "false")
            text = open(p).read()
            self.assertIn("# comment", text)
            self.assertIn("gamemode=survival", text)
            self.assertEqual(read_properties(p)["white-list"], "false")

    def test_appends_when_absent(self):
        from mcsadmin.server import read_properties, set_property

        with tempfile.TemporaryDirectory() as d:
            p = self._path(d)
            set_property(p, "motd", "hello")
            self.assertEqual(read_properties(p)["motd"], "hello")
            self.assertEqual(read_properties(p)["white-list"], "true")

    def test_creates_file_if_missing(self):
        from mcsadmin.server import read_properties, set_property

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "new.properties")
            set_property(p, "white-list", "false")
            self.assertEqual(read_properties(p)["white-list"], "false")


class TestWhitelistFiles(unittest.TestCase):
    def test_add_remove_read_roundtrip(self):
        from mcsadmin.server import (
            add_whitelist_entry,
            read_whitelist_file,
            remove_whitelist_entry,
        )
        from mcsadmin.util import offline_uuid

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "whitelist.json")
            self.assertTrue(add_whitelist_entry(p, "Alex"))
            self.assertFalse(add_whitelist_entry(p, "Alex"))  # no dupes
            self.assertTrue(add_whitelist_entry(p, "Bob"))
            data = read_whitelist_file(p)
            self.assertEqual(data, ["Alex", "Bob"])
            self.assertEqual(
                offline_uuid("Steve"), "5627dd98-e6be-3c21-b8a8-e92344183641"
            )
            self.assertTrue(remove_whitelist_entry(p, "Alex"))
            self.assertEqual(read_whitelist_file(p), ["Bob"])

    def test_read_missing_file(self):
        from mcsadmin.server import read_whitelist_file

        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                read_whitelist_file(os.path.join(d, "nope.json")), []
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)