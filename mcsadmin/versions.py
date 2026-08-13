"""Minecraft version discovery and server.jar installation.

Uses Mojang's public version manifest (version_manifest_v2.json) so the
tool can fetch the latest release, a specific version, or list all
available ones without hard-coding any download URLs.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from .util import download_with_progress

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
USER_AGENT = "MCSAdmin/1.0 (terminal minecraft server manager)"
MARKER_FILE = ".mcsadmin-version"
ALIAS_FILE = ".mcsadmin-aliases.json"

Progress = Callable[[int, int], None]  # (downloaded, total)


@dataclass
class VersionInfo:
    id: str
    type: str  # "release" or "snapshot"
    url: str  # version detail json


@dataclass
class Manifest:
    latest_release: str
    latest_snapshot: str
    versions: List[VersionInfo] = field(default_factory=list)


def _read_url(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_manifest(timeout: float = 30.0) -> Manifest:
    data = json.loads(_read_url(MANIFEST_URL, timeout))
    versions = [
        VersionInfo(v["id"], v.get("type", "release"), v["url"])
        for v in data.get("versions", [])
    ]
    latest = data.get("latest", {})
    return Manifest(
        latest_release=latest.get("release", ""),
        latest_snapshot=latest.get("snapshot", ""),
        versions=versions,
    )


def resolve_version(
    manifest: Manifest, selection: Optional[str]
) -> Optional[VersionInfo]:
    """Map requested version id/alias to a VersionInfo.

    Accepts: None/"latest"/"release" -> latest release,
    "snapshot" -> latest snapshot, or an exact id.
    """
    if selection is None or selection in ("latest", "release"):
        target = manifest.latest_release
    elif selection == "snapshot":
        target = manifest.latest_snapshot
    else:
        target = selection
    for v in manifest.versions:
        if v.id == target:
            return v
    # fall back to fuzzy match (e.g. "1.21")
    matches = [v for v in manifest.versions if v.id.startswith(target)]
    if matches:
        matches.sort(key=lambda v: v.id, reverse=True)
        for m in matches:
            if m.type == "release":
                return m
        return matches[0]
    return None


def version_detail(version: VersionInfo, timeout: float = 30.0) -> dict:
    """Fetch the version detail JSON (server download + java version)."""
    return json.loads(_read_url(version.url, timeout))


def server_jar_url(version: VersionInfo, timeout: float = 30.0) -> str:
    detail = version_detail(version, timeout)
    try:
        return detail["downloads"]["server"]["url"]
    except KeyError as exc:
        raise ValueError(f"No server download available for '{version.id}'.") from exc


def download_file(
    url: str,
    dest: str,
    progress: Optional[Progress] = None,
    timeout: float = 120.0,
) -> int:
    """Stream-download a file, reporting progress. Returns bytes written."""
    return download_with_progress(url, dest, progress=progress, timeout=timeout)


def read_installed_version(server_dir: str) -> Optional[str]:
    """Return the recorded version id of the installed server.jar."""
    try:
        with open(
            os.path.join(server_dir, MARKER_FILE), "r", encoding="utf-8"
        ) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def write_installed_version(server_dir: str, version_id: str) -> None:
    try:
        with open(
            os.path.join(server_dir, MARKER_FILE), "w", encoding="utf-8"
        ) as fh:
            fh.write(f"{version_id}\n")
    except OSError:
        pass


def _world_base(server_dir: str) -> str:
    """Return the active world folder name (server.properties level-name)."""
    props = os.path.join(server_dir, "server.properties")
    if os.path.exists(props):
        try:
            with open(props, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("level-name="):
                        return line.partition("=")[2].strip() or "world"
        except OSError:
            pass
    return "world"


def _read_aliases(server_dir: str) -> dict:
    try:
        with open(
            os.path.join(server_dir, ALIAS_FILE), "r", encoding="utf-8"
        ) as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_aliases(server_dir: str, aliases: dict) -> None:
    try:
        with open(
            os.path.join(server_dir, ALIAS_FILE), "w", encoding="utf-8"
        ) as fh:
            json.dump(aliases, fh, indent=2)
    except OSError:
        pass


def _move_dir(src: str, dst: str) -> None:
    """Move a folder, replacing any conflicting destination."""
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.rename(src, dst)


def reorg_worlds(
    server_dir: str,
    old_version: str,
    new_version: str,
    alias: Optional[str] = None,
) -> None:
    """Keep each version's worlds separate by rotating the active folder.

    Minecraft worlds are not compatible across server versions, so running a
    differently-versioned jar against an old world corrupts it. Following the
    requested scheme ("world" <-> "<version>world"), when switching from
    old_version to new_version the current world folder is stashed as
    "<old_version><base>" and the new version's stash, if any, is restored to
    the active "<base>" name.

    ``base`` is the level-name from server.properties (default "world").

    ``alias`` is set when the target came from a moving alias ("latest",
    "release", "snapshot"). The resolved id of an alias drifts as new
    releases arrive, so the stash it would look for may not exist yet; in
    that case the world previously recorded as the latest build is restored
    instead. That keeps an upgrade from silently producing an empty world
    (the "my data was wiped" bug).
    """
    if old_version == new_version:
        return
    base = _world_base(server_dir)
    active = os.path.join(server_dir, base)
    aliases = _read_aliases(server_dir)
    prev_latest = aliases.get("latest")
    if old_version and os.path.isdir(active):
        _move_dir(active, os.path.join(server_dir, f"{old_version}{base}"))
    target = new_version
    if alias:
        cand = os.path.join(server_dir, f"{target}{base}")
        if not os.path.isdir(cand):
            prev = prev_latest or aliases.get("last")
            if prev and os.path.isdir(os.path.join(server_dir, f"{prev}{base}")):
                target = prev
        aliases["latest"] = new_version
    incoming = os.path.join(server_dir, f"{target}{base}")
    if os.path.isdir(incoming):
        _move_dir(incoming, os.path.join(server_dir, base))
    _write_aliases(server_dir, aliases)


def install_server_jar(
    server_dir: str,
    selection: Optional[str] = None,
    progress: Optional[Progress] = None,
    timeout: float = 120.0,
) -> Tuple[str, str]:
    """Ensure server.jar exists for the chosen version.

    Returns (version_id, jar_path). Raises on network/download errors.

    Downloads whenever the jar is missing or the resolved target version
    differs from the one recorded in the marker file (so switching back to
    an older version — or up to the latest release with 'install latest' —
    really replaces server.jar and moves the worlds over).
    """
    os.makedirs(server_dir, exist_ok=True)
    jar_path = os.path.join(server_dir, "server.jar")
    manifest = fetch_manifest()
    version = resolve_version(manifest, selection)
    if version is None:
        raise ValueError(f"Version '{selection}' not found.")
    url = server_jar_url(version)
    installed = read_installed_version(server_dir)
    jar_missing = not (os.path.exists(jar_path) and os.path.getsize(jar_path) > 0)
    if jar_missing or installed != version.id:
        if progress:
            progress(0, 0)
        download_file(url, jar_path, progress=progress, timeout=timeout)
        if installed != version.id:
            # Rotate worlds only once the new jar is safely in place so a
            # failed download never leaves an old jar on a new version's world.
            alias = "latest" if selection in ("latest", "release") else (
                "snapshot" if selection == "snapshot" else None)
            reorg_worlds(server_dir, installed, version.id, alias)
        write_installed_version(server_dir, version.id)
    return version.id, jar_path


def install_with_java(
    server_dir: str,
    selection: Optional[str] = None,
    progress: Optional[Progress] = None,
    timeout: float = 120.0,
) -> Tuple[str, str, str, int]:
    """Install server.jar plus a compatible JRE.

    Returns (version_id, jar_path, java_bin, required_major). The JRE is
    only fetched when the system Java cannot run the jar; otherwise it
    falls back to the system 'java'. Raises on failures.
    """
    from . import javavm

    v_id, jar = install_server_jar(server_dir, selection, progress=progress,
                                   timeout=timeout)
    manifest = fetch_manifest()
    ver = resolve_version(manifest, v_id)
    detail = version_detail(ver) if ver else None
    required, src = javavm.required_java_major(jar, detail)

    system_java = javavm.system_java_bin()
    system_major = (
        javavm.installed_java_major(system_java) if system_java else None
    )
    if system_java and system_major and system_major >= required:
        return v_id, jar, system_java, required
    # fetch a JRE
    jvm_dir = os.path.join(server_dir, ".vms")
    java_bin, report = javavm.install_jre(
        jvm_dir, required, progress=progress, timeout=timeout
    )
    return v_id, jar, java_bin, required


def list_versions(selection: Optional[str] = None, limit: int = 20) -> List[str]:
    manifest = fetch_manifest()
    if selection:
        target = resolve_version(manifest, None)
        return [f"{target.id} ({target.type})"] if target else []
    releases = [v for v in manifest.versions if v.type == "release"]
    snaps = [v for v in manifest.versions if v.type == "snapshot"]
    chosen = (releases + snaps)[:limit]
    return [f"{v.id} ({v.type})" for v in chosen]


def download_async(
    server_dir: str,
    selection: Optional[str],
    on_progress: Progress,
    on_done: Callable[[bool, Optional[str], str], None],
    timeout: float = 120.0,
    with_java: bool = False,
) -> threading.Thread:
    """Kick off an install in a background thread."""

    def _run() -> None:
        try:
            if with_java:
                v_id, _jar, _java, _req = install_with_java(
                    server_dir, selection, progress=on_progress, timeout=timeout
                )
            else:
                v_id, _jar = install_server_jar(
                    server_dir, selection, progress=on_progress, timeout=timeout
                )
            on_done(True, v_id, "ok")
        except (urllib.error.URLError, ValueError, OSError) as exc:
            on_done(False, None, str(exc))

    t = threading.Thread(target=_run, daemon=True, name="mc-installer")
    t.start()
    return t