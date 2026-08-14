# MCSAdmin

A portable, terminal-based Minecraft server manager with a curses TUI.

> **Running Bedrock instead?** See **MCBSAdmin**, the sibling project that
> manages a Minecraft **Bedrock** Dedicated Server (native `bedrock_server`,
> no JVM): <https://github.com/Polaricito/MCBSAdmin>

MCSAdmin lets you install a Minecraft Java server (latest release or a
version of your choice), boot and manage it, watch CPU/RAM usage, see who
is connected, and run console commands — all from one adaptive TUI window.
It has **zero third-party Python dependencies** (only the standard library),
so it runs on essentially any Linux box and installs cleanly from a git
checkout.

## Features

- **Install any version** — fetches Mojang's public version manifest and
  downloads `server.jar`; pick the latest release, a snapshot, or an exact
  version id from an interactive picker.
- **Managed Java runtime** — when the system JVM is missing or too old for
  the selected server, `install --with-java` downloads a matching Temurin
  JRE automatically (class-file–version aware).
- **Adaptive TUI** — the layout rearranges itself to the terminal size
  (console + side-by-side panels on wide terminals, stacked on narrow ones).
- **Distinct color tones** — lighter shade for the server console, darker
  shade for the player list, accent colors for status and resources.
- **Live monitoring** — system and per-process CPU (from `/proc/stat`) and
  memory (`/proc/meminfo`, `VmRSS`) with zero external tools, drawn as
  fill bars alongside a players-bar (e.g. `##### 10/20`).
- **Connected players** — an RCON-backed poller auto-refreshes the list;
  console join/leave lines also update it instantly.
- **Console commands** — type straight into the server console, or use local
  commands such as `/stop`, `/restart`, `/players`, `/help`.

## Requirements

- Linux (reads `/proc`)
- Python 3.8+
- A Java runtime for the server (auto-installed with `--with-java`)
- A terminal that supports 256 colors (`TERM=xterm-256color` etc.)

## Dependencies

- Python 3.8+ (only the standard library is used — no third-party packages)
- A terminal emulator that supports 256 colors and curses (GNOME Terminal,
  Konsole, kitty, Alacritty, tmux/screen, or a plain Linux TTY)
- No third-party Python packages; only the standard library (`curses`,
  `subprocess`, `threading`, `json`, …)

## Install

```sh
# run without installing, from a checkout:
python3 -m mcsadmin --help

# install as a system command:
python3 -m pip install --user --break-system-packages .
mcsadmin --help
```

`--break-system-packages` is required on distros that mark pip as externally
managed (Arch, recent Debian/Ubuntu) — it installs into your user profile
instead of the system Python. On those the `mcsadmin` command lands in
`~/.local/bin`, so make sure it's on your `PATH`:

```sh
# bash / zsh
export PATH="$PATH:$HOME/.local/bin"

# fish
set -U fish_user_paths "$HOME/.local/bin"
```

Arch users: install straight from this repository —

```sh
python3 -m pip install --user --break-system-packages git+https://github.com/Polaricito/MCSAdmin.git
mcsadmin --help
```

## Quick start

```sh
mcsadmin install --with-java    # latest release + compatible JRE if needed
mcsadmin                        # launch the TUI
```

Inside the TUI, press `S` to start the server. That's it.

### TUI control

| Key             | Action                                  |
|-----------------|-----------------------------------------|
| `S`             | start the server                        |
| `X` / `R`       | stop / restart the server               |
| `I`             | install the latest release build        |
| `E`             | server settings (description / icon / RAM / cores) — requires server stopped |
| `V`             | worlds (add / rename / delete / switch) — requires server stopped |
| `W`             | world options (difficulty, gamemode, pvp, …) — requires server stopped |
| `PgUp` / `PgDn` | scroll the console                      |
| `H`             | show key bindings and local commands    |
| `Q`             | quit (stops the server first)           |

All shortcuts are **UPPERCASE** so any lowercase text you type goes straight
to the server console instead of triggering a hotkey.

Any other text + `Enter` is sent straight to the Minecraft server console.
Local control commands start with `/`: `/start`, `/stop`, `/restart`,
`/install [version]`, `/versions`, `/players`, `/help`.

### Server icon

The vanilla client only shows a `server-icon.png` that is exactly 64x64.
From the settings screen (`E`) you can cycle discovered images or type/paste
a path directly into the icon field — MCSAdmin resizes any PNG to 64x64 in
pure Python and writes a valid icon into the server directory. Because the
icon pipeline itself is PNG-native, ImageMagick is **optional**: install it
(`pacman -S imagemagick`) to also convert JPEG/JPG icons; without it, JPEGs
are rejected with a hint.

### Resource bars

The resources pane renders each measurable resource as a three-line block —
the fill bar on top, the name underneath, then the value, e.g. for the
server RAM at 25% of the box:

```
[########--------------]
Server RAM
900 MiB / 4 GiB
```

CPU rows show the percentage and RAM rows show live usage as
`used / total` (`900 MiB / 4 GiB`), so the server's own RSS and the whole
system's memory are directly comparable.

When the server is up, the resources pane pins a network/uptime block to
the bottom, with the machine's **public IP** and the configured game
**port** on the left and the uptime on the right:

```
203.0.113.9                      14:32
25565                           Uptime
```

The IP is resolved once at startup (ipify, falling back to the outbound
interface address) and cached; the port mirrors the `gameport` you set in
server settings.

The player count lives under the `PLAYERS` header (as `PLAYERS [10/20]`),
authoritatively re-synced over RCON every 5 seconds — the max comes from
`server.properties` (or the World Options screen) with vanilla's 20 as the
fallback. Each player row also shows the connection IP the console logs on
login. When a panel is too short for the full form it degrades to the
compact two-line / single-line layout.

### Player actions: kick / ban / IP ban

Clicking a player opens a menu with `Kick player`, `Ban player` and
`IP ban` (bans the address the console logged for them — `ban-ip`). Each
action then asks for an optional reason in a small form with a visible
text field and `[send]` / `[cancel]` buttons — type the reason and hit
**Enter** or click `[send]`; leave it blank to kick/ban without a reason.
**Esc** steps back (from the reason form to the player menu).

### Whitelist

The whitelist editor lives in **World Options** (`W`), as the `whitelist`
row — **click it** (or press **Enter**/**Right arrow**) to open the current
list, with an `Add` button that prompts for a name (`whitelist add`) and a
**Disable whitelist**/**Enable whitelist** button that flips `white-list` in
`server.properties` without touching the entries. A click on any listed name
removes it (`whitelist remove`). The row is marked with a trailing `>` while
the whitelist is active (`white-list: true`) and shows plain `whitelist`
without one when it isn't. ESC steps back to the World Options list.

### Server settings, RAM / cores and world options

`E` (or the `[E] settings` footer button) opens the server settings screen,
and `W` opens world options. Both refuse to open while the server is
running with a "Stop server first." notice, and the `[W] world` button
disappears from the bottom bar while it runs. The running bar is
`[X] stop [R] restart [H] help [Q] quit`; when stopped it is
`[S] start [I] install [E] settings [V] worlds [W] world [H] help [Q] quit`.

Its `desc` / `icon` fields work as above, plus one sub-menu:

- **`jvm:` Java settings** — set minimum/maximum RAM (MiB) and CPU cores.
  Cores translates into `-XX:ActiveProcessorCount=N`; leave it blank for
  auto. RAM becomes `-Xms/-Xmx` as before. Values apply on the next start.
  Saving the settings screen without touching the icon keeps the current
  server image (it no longer reverts when the icon isn't among the images
  auto-discovered for cycling).

World options have their **own key, `W`** (a `[W] world` entry no longer
hides inside server settings):

- difficulty (peaceful/easy/normal/hard), gamemode, max players, view
  distance, pvp, hardcore, spawn monsters / animals, command blocks, spawn
  protection, structures, flight and **online mode**. Set
  `online mode` to `false` for an **offline** server — players join
  without a Mojang account — instead of the default `true`. Cycle choices
  with Enter, type numbers for the numeric fields, and reach `done` below
  the list to save. A `default` value leaves that property untouched;
  otherwise the chosen value overrides `server.properties` (on this start
  and every future write). The `whitelist` row opens the whitelist editor
  and carries a trailing `>` while `white-list` is enabled.

### Worlds

`V` (or the `[V] worlds` footer button) opens the **Worlds** selector — the
menu that owns *which world is loaded*, distinct from **World Options**
(`W`, which tunes one world's properties). Every `level.dat` folder in the
server directory is shown as a card, with `*` marking the currently active
one. Clicking a card selects it; the bottom bar then offers `[switch]`,
`[rename]`, `[del]` and `[done]`, plus `[add]` to create a brand-new world
(which becomes active). The same flows work from the keyboard: **Enter**
switches, `a` adds, `r` renames, `d` deletes (with a confirm), **Esc** back.
Renames use `os.replace`, deletes ask for confirmation first, and switching
only takes effect with the server stopped. A brand-new empty world is
generated by the server on its first start.

### Older server versions

Netty's native transport aborts startup on old versions (they don't ship
the epoll library), so `use-native-transport=false` is written into
`server.properties`. If it is already set to `false` it is left alone;
any other value gets rewritten to `false`. On JDK 17+ the launch also
passes `--enable-native-access=ALL-UNNAMED` to silence JEP 412 native-
access errors; the option is only added when the detected JVM understands
it, so Java 8/11 servers (1.8.9-era) still boot.

### Command line

```
mcsadmin                       launch the TUI (default)
mcsadmin install               latest release server.jar
mcsadmin install 1.21.1        a specific version
mcsadmin install --with-java   also fetch a compatible JRE if needed
mcsadmin versions              list versions fetched from Mojang
mcsadmin status                show install / config status
mcsadmin --data-dir DIR ...    store config + server data in DIR
```

## Configuration

Everything lives in a single JSON file, so the whole manager is movable:
`~/.config/mcsadmin/config.json` (or `$XDG_CONFIG_HOME/mcsadmin/config.json`,
overridable with `MCSADMIN_CONFIG`). MCSAdmin never derives its data location
from the install prefix, so a system install under `/usr/bin` and
`/usr/share` still stores per-user state under the home directory.

To move everything to an explicit, writable location (e.g. when installed
system-wide or running in a restricted environment):

```sh
mcsadmin --data-dir ~/.local/share/mcsadmin status
mcsadmin --data-dir ~/.local/share/mcsadmin install --with-java
```

`MCSADMIN_DATA_DIR` sets the same base for both the config file and the
default server directory.

```jsonc
{
  "server_dir": "~/.config/mcsadmin/server",
  "version": "26.2",
  "level": "world",
  "java": { "path": null, "required": 25 },
  "java_flags": { "min_memory_mb": 1024, "max_memory_mb": 2048, "cores": null, "extra": "" },
  "world": { "difficulty": "hard", "pvp": "false" },
  "rcon": { "enabled": true, "password": "auto-generated", "port": 25575 },
  "gameport": 25565,
  "motd": "MCSAdmin managed server"
}
```

The server jar, world save, `server.properties`, `eula.txt` and any managed
JRE (`server/.vms/`) live in `server_dir`. Copy/modify the config to move a
whole installation between machines.

## Portability notes

- Uses only the Python standard library; no `pip` deps to conflict.
- `curses` ships with CPython on all POSIX systems (Arch needs no extra
  packages).
- `/proc` parsing degrades gracefully on other OSes.
- `install --with-java` keeps the deployment self-contained when a modern
  JVM isn't present (useful inside containers or minimal VPS images).

## Installing from GitHub

The repository is a standard `pyproject.toml` package, so you can install it
straight from a git checkout with no extra steps:

```sh
# latest commit on the default branch
python3 -m pip install --user --break-system-packages git+https://github.com/Polaricito/MCSAdmin.git

# a specific tag
python3 -m pip install --user --break-system-packages git+https://github.com/Polaricito/MCSAdmin.git@v1.0.0
```

The `--user --break-system-packages` combo targets distros with an externally
managed Python (Arch, recent Debian/Ubuntu); drop the flag if your system
still allows plain `pip install`. A `--user` install puts the `mcsadmin`
command in `~/.local/bin` — add it to your `PATH` if the command isn't found:

```sh
# bash / zsh
export PATH="$PATH:$HOME/.local/bin"

# fish
set -U fish_user_paths "$HOME/.local/bin"
```

Or clone and install in editable mode for development:

```sh
git clone git@github.com:Polaricito/MCSAdmin.git
cd MCSAdmin
python3 -m pip install --user --break-system-packages -e .
```

## Updating

MCSAdmin ships as one Python package, so updating is just re-installing the
latest commit from GitHub:

```sh
# re-run the same install command; pip pulls the newest code
python3 -m pip install --user --break-system-packages git+https://github.com/Polaricito/MCSAdmin.git
```

The `--user` cache is keyed by version, and the git source changes on every
push, so force a fresh fetch + install to be sure you're on the newest code:

```sh
python3 -m pip install --user --break-system-packages \
  --upgrade --force-reinstall \
  git+https://github.com/Polaricito/MCSAdmin.git
```

If you installed a specific tag, add `@vX.Y.Z` to the URL and drop
`--force-reinstall` for the same version.

Editable installs (development) update with a plain `git pull` inside the
checkout:

```sh
cd MCSAdmin
git pull
```

Your server data and config (`~/.config/mcsadmin/`) are untouched by updates.
Check your current version any time with `mcsadmin --version`.

## Development

```sh
python3 -m unittest discover -s tests -v   # run unit tests
python3 -m py_compile mcsadmin/*.py
```

## License

MIT — see `LICENSE`. MCSAdmin is not affiliated with Mojang or Microsoft;
"Minecraft" is a trademark of Mojang Synergies AB.
