"""Black-and-white theme for the TUI.

No colors: panels are distinguished by borders, and emphasis is done with
the plain curses attributes A_REVERSE / A_BOLD / A_DIM. This is immune to
custom terminal color schemes and works on any terminal.
"""

from __future__ import annotations

import curses
from typing import Dict

# (name -> curses attribute)
ATTRS: Dict[str, int] = {
    "console": 0,
    "console_accent": curses.A_BOLD,
    "players": 0,
    "players_accent": curses.A_BOLD,
    "stats": 0,
    "stats_accent": curses.A_BOLD,
    "header": curses.A_REVERSE,   # solid inverted header bar
    "status_ok": curses.A_BOLD,   # running
    "status_warn": 0,             # starting / stopping
    "status_err": curses.A_DIM,   # stopped / errors
    "input": 0,
    "input_accent": curses.A_BOLD,
    "border": 0,
    "dim": curses.A_DIM,
}


class Theme:
    """Resolves named roles to curses attributes (color pairs unused)."""

    def __init__(self) -> None:
        self.initialized = False

    def init(self, stdscr) -> None:
        if self.initialized:
            return
        self.initialized = True
        if curses.has_colors():
            try:
                curses.start_color()
                if curses.COLORS and curses.COLORS >= 256:
                    curses.use_default_colors()
                # keep all color pairs untouched; everything uses 0
            except curses.error:
                pass
        try:
            stdscr.refresh()
        except curses.error:
            pass

    def pair(self, name: str) -> int:
        return 0

    def attr(self, name: str, extra: int = 0) -> int:
        return ATTRS.get(name, 0) | (extra & ~curses.A_COLOR)

    # -- convenience status lookups --------------------------------
    def status_pair(self, status: str) -> int:
        if status == "running":
            return self.attr("status_ok")
        if status in ("starting", "stopping"):
            return self.attr("status_warn")
        return self.attr("status_err")


# Single shared instance.
theme = Theme()