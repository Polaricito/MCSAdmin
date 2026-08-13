"""Configuration management for MCSAdmin.

Everything lives in a single config file under a platform-appropriate
location (XDG_CONFIG_HOME on Linux, which also keeps uploaded saves and
jars together so the whole thing is portable via a single directory).

The data directory is deliberately user-owned: it defaults to
``$XDG_CONFIG_HOME/mcsadmin`` (or ``~/.config/mcsadmin``) and never to
anything derived from the install prefix (``/usr``), the Python module
location or the current working directory. When the package is installed
system-wide (``/usr/bin`` + ``/usr/share``) the app therefore stores its
state under the user's home directory instead of trying to write into the
(read-only) install tree. ``MCSADMIN_DATA_DIR`` overrides the base for
both the config file and the default server directory, and ``--data-dir``
on the CLI sets it explicitly.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from typing import Any, Dict, Optional

DEFAULTS: Dict[str, Any] = {
    "server_dir": None,          # directory holding server.jar world etc.
    "version": None,             # selected Minecraft version id
    "jar_url": None,             # downloaded jar URL (cached)
    "server_icon": None,         # source path of server-icon.png
    "java": {
        "path": None,  # explicit java binary override
    },
    "java_flags": {
        "min_memory_mb": 1024,
        "max_memory_mb": 2048,
        "cores": None,            # None == auto (no -XX:ActiveProcessorCount)
        "extra": "",
    },
    "world": {},                  # server.properties overrides (World Options)
    "rcon": {
        "enabled": True,
        "password": None,        # auto-generated on install if empty
        "port": 25575,
    },
    "gameport": 25565,
    "motd": "MCSAdmin managed server",
}


def default_config_dir() -> str:
    """Return the directory where MCSAdmin stores its state.

    Resolution order:
      1. ``MCSADMIN_DATA_DIR`` (explicit override; must be absolute)
      2. ``$XDG_CONFIG_HOME/mcsadmin`` (absolute)
      3. ``~/.config/mcsadmin``
      4. a subdir of the system temp dir as a last resort

    The result is always an absolute path. Crucially, an unset/missing
    ``HOME`` or a non-absolute ``XDG_CONFIG_HOME`` must never make the app
    fall back to a relative path such as ``~/.config`` — that would create
    a literal ``~`` folder in the current working directory (which for a
    system install can be ``/usr/bin`` or ``/usr/share``).
    """
    override = os.environ.get("MCSADMIN_DATA_DIR")
    if override and os.path.isabs(override):
        return os.path.abspath(override)

    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        return os.path.abspath(os.path.join(xdg, "mcsadmin"))

    home = os.path.expanduser("~")
    if home in ("", "~") or not os.path.isabs(home):
        home = os.environ.get("HOME")
    if home and os.path.isabs(home):
        return os.path.join(home, ".config", "mcsadmin")

    return os.path.join(tempfile.gettempdir(), "mcsadmin")


class Config:
    """Load/save the JSON config file."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = (
            path
            or os.environ.get("MCSADMIN_CONFIG")
            or os.path.join(default_config_dir(), "config.json")
        )
        # deep-copy: nested dicts (java_flags, rcon, world) must never be
        # shared between Config instances, or in-place edits (e.g. saving a
        # new RAM/cores value) would leak back into the module DEFAULTS.
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    for key in DEFAULTS:
                        if key in loaded:
                            self.data[key] = loaded[key]
            except (ValueError, OSError):
                pass

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def server_dir(self) -> str:
        d = self.data.get("server_dir")
        if d:
            return os.path.expanduser(d)
        sv = os.path.join(default_config_dir(), "server")
        self.data["server_dir"] = sv
        self.save()
        return sv

    @property
    def java(self) -> Dict[str, Any]:
        return self.data.setdefault("java_flags", {})

    @property
    def rcon(self) -> Dict[str, Any]:
        return self.data.setdefault("rcon", {})


def generate_password(length: int = 16) -> str:
    """Generate a random ASCII password for RCON."""
    import random
    import string

    alphabet = string.ascii_letters + string.digits
    rng = random.SystemRandom()
    return "".join(rng.choice(alphabet) for _ in range(length))