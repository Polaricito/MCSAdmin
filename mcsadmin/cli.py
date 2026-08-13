"""Command-line interface for MCSAdmin.

Usage:
    mcsadmin                 Launch the TUI (default).
    mcsadmin tui             Explicit TUI launch.
    mcsadmin install         Install the latest release server.jar.
    mcsadmin install 1.21    Install a specific version.
    mcsadmin versions        List a few known versions.
    mcsadmin status          Show install/config status.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from . import __version__
from .config import Config, generate_password


def _curses_wrapper():
    import curses

    try:
        from .tui import App

        def _main(stdscr):
            config = Config()
            app = App(stdscr, config)
            app.run()

        curses.wrapper(_main)
    except ImportError:
        sys.stderr.write("curses is not available on this platform.\n")
        return 1
    return 0


def _progress(done: int, total: int) -> None:
    if total:
        pct = done / total * 100
        bar = "#" * int(pct // 2)
        msg = f"  \rDownloading [{bar:<50}] {pct:5.1f}%"
    else:
        msg = f"  \rDownloading {done / 1048576:.1f} MiB"
    sys.stderr.write(msg)
    sys.stderr.flush()
    if done == total and total:
        sys.stderr.write("\n")


def _apply_data_dir(data_dir: Optional[str]) -> None:
    """Point config and the default server dir at an explicit location.

    Prevents a system-installed app (``/usr/bin`` + ``/usr/share``) from
    ever writing into the install tree: the user picks a writable spot and
    both the JSON config and server data land there.
    """
    if not data_dir:
        return
    dd = os.path.abspath(os.path.expanduser(data_dir))
    try:
        os.makedirs(dd, exist_ok=True)
    except OSError as exc:
        sys.stderr.write(f"--data-dir not usable: {exc}\n")
        raise SystemExit(2)
    os.environ.setdefault("MCSADMIN_DATA_DIR", dd)
    os.environ.setdefault("MCSADMIN_CONFIG", os.path.join(dd, "config.json"))


def cmd_install(config: Config, version: Optional[str], with_java: bool = False) -> int:
    from .server import ServerManager
    from .util import LogBuffer
    from .versions import install_server_jar, install_with_java

    server_dir = config.server_dir()
    os.makedirs(server_dir, exist_ok=True)
    # Set an RCON password up front so server.properties is sane.
    if not config.rcon.get("password"):
        config.rcon["password"] = generate_password()
        config.save()

    label = version or "latest release"
    print(f"MCSAdmin: installing {label} into {server_dir}")
    try:
        if with_java:
            v_id, jar, java_bin, required = install_with_java(
                server_dir, version, progress=_progress, timeout=300.0
            )
            config.data.setdefault("java", {})["path"] = java_bin
            config.data["java"]["required"] = required
            config.save()
            print(f"Using JRE: {java_bin} (Java {required}+)")
        else:
            v_id, jar = install_server_jar(
                server_dir, version, progress=_progress, timeout=120.0
            )
    except Exception as exc:  # noqa: BLE001
        print(f"Install failed: {exc}")
        return 1
    config.set("version", v_id)
    print(f"Installed {v_id}: {jar}")
    # Write eula + properties so the server can be started right away.
    mgr = ServerManager(config, LogBuffer())
    mgr.setup_files()
    print("Wrote eula.txt and server.properties.")
    print("Launch the manager with 'mcsadmin' and press 'S' to start.")
    return 0


def cmd_versions(_config: Config) -> int:
    from .versions import list_versions

    try:
        names = list_versions(limit=24)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not fetch versions: {exc}")
        return 1
    for name in names:
        print(name)
    return 0


def cmd_status(config: Config) -> int:
    server_dir = config.server_dir()
    jar = os.path.join(server_dir, "server.jar")
    data_dir = os.path.dirname(os.path.abspath(config.path))
    print(f"data dir      : {data_dir}")
    print(f"config        : {config.path}")
    print(f"server dir    : {server_dir}")
    print(f"version       : {config.get('version') or 'not installed'}")
    print(f"server.jar    : {'present' if os.path.exists(jar) else 'missing'}")
    print(f"port          : {config.get('gameport', 25565)}")
    rpass = config.rcon.get("password")
    print(f"rcon password : {'set' if rpass else 'not set'}  port={config.rcon.get('port', 25575)}")
    java_flags = config.java
    print(
        f"memory        : {java_flags.get('max_memory_mb', java_flags.get('min_memory_mb', 2048))}M assigned"
    )
    if not os.path.isdir(server_dir):
        try:
            os.makedirs(server_dir, exist_ok=True)
        except OSError:
            pass
    if not os.access(server_dir, os.W_OK):
        print(
            f"! server dir is NOT writable ({server_dir}) — "
            "use --data-dir to point MCSAdmin at a writable location."
        )
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcsadmin",
        description="Terminal-based Minecraft server manager.",
    )
    parser.add_argument("-v", "--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    p_tui = sub.add_parser("tui", help="Run the interactive TUI (default)")
    p_tui.add_argument("--config", help="Path to a custom config JSON")
    p_tui.add_argument("--data-dir", help="Directory for config + server data")

    p_install = sub.add_parser("install", help="Install server.jar")
    p_install.add_argument("version", nargs="?", help="Version id or 'latest'/'snapshot'")
    p_install.add_argument("--config", help="Path to a custom config JSON")
    p_install.add_argument(
        "--data-dir", help="Directory for config + server data"
    )
    p_install.add_argument(
        "--with-java", action="store_true",
        help="Also fetch a compatible JRE (Temurin) when the system Java is too old",
    )

    sub.add_parser("versions", help="Show available versions")

    p_status = sub.add_parser("status", help="Show install/config status")
    p_status.add_argument("--config", help="Path to a custom config JSON")
    p_status.add_argument("--data-dir", help="Directory for config + server data")

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "config", None):
        os.environ.setdefault("MCSADMIN_CONFIG", args.config)
    _apply_data_dir(getattr(args, "data_dir", None))

    command = args.command or "tui"
    if command == "tui":
        return _curses_wrapper()
    if command == "install":
        return cmd_install(Config(), args.version, getattr(args, "with_java", False))
    if command == "versions":
        return cmd_versions(Config())
    if command == "status":
        return cmd_status(Config())
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())