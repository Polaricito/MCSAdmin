"""Download and manage a compatible Java runtime for the server.

Vanilla server jars pin a Java major version (via the manifest's
``javaVersion`` block, or inferred from the jar's class file version).
When the system JVM is missing or too old, MCSAdmin can fetch a Temurin
JRE from the Adoptium API and run the server from it — this keeps the
tool usable on machines without a modern JVM installed.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import struct
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from typing import Optional, Tuple

from .util import download_with_progress

API = "https://api.adoptium.net/v3/assets/latest/{major}/hotspot"
UA = "MCSAdmin/1.0 (terminal minecraft server manager)"

ARCH_MAP = {"x86_64": "x64", "amd64": "x64", "aarch64": "aarch64", "arm64": "aarch64"}


def _class_to_feature(class_major: int) -> int:
    """Class file major 52 == Java 8, so feature = major - 44."""
    return max(0, class_major - 44)


def java_major_from_jar(jar_path: str) -> Optional[int]:
    """Infer the Java major version the jar targets from class files."""
    try:
        with zipfile.ZipFile(jar_path) as zf:
            for name in zf.namelist():
                if name.endswith(".class"):
                    data = zf.read(name)
                    if len(data) >= 8:
                        class_major = struct.unpack(">H", data[6:8])[0]
                        return _class_to_feature(class_major)
        return None
    except (zipfile.BadZipFile, OSError):
        return None


def required_java_major(jar_path: str, detail: Optional[dict] = None) -> Tuple[
    int, str
]:
    """Return (major, source) for the Java version this jar needs.

    ``detail`` is the version JSON from Mojang; falls back to inspecting
    the jar's class file version.
    """
    if detail:
        jv = detail.get("javaVersion")
        if jv and isinstance(jv, dict):
            major = jv.get("majorVersion")
            if major:
                return int(major), "manifest"
    got = java_major_from_jar(jar_path)
    if got:
        return got, "class-file"
    return 17, "default"


def installed_java_major(java_bin: str) -> Optional[int]:
    """Return the major for a java binary, or None."""
    import subprocess

    try:
        out = subprocess.run(
            [java_bin, "-version"], capture_output=True, text=True, timeout=15
        )
        blob = (out.stdout + out.stderr).lower()
        for marker in ('"1.', '"'):
            idx = blob.find(marker)
            if idx >= 0:
                start = idx + 1
                # e.g. 21.0.1+11 or 17.0.7
                num = ""
                for ch in blob[start:]:
                    if ch.isdigit():
                        num += ch
                    else:
                        break
                if num:
                    return int(num)
        # dot-version format: "openjdk version 17.0.7"
        import re

        m = re.search(r"version\s+\"(\d+)", blob)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def find_latest_jre(major: int) -> str:
    """Query Adoptium for the latest JRE download link for a Java major."""
    arch = ARCH_MAP.get(platform.machine().lower(), "x64")
    url = f"{API.format(major=major)}?architecture={arch}&heap_size=normal&image_type=jre&jvm_impl=hotspot&os=linux&vendor=eclipse"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    asset = data[0]
    package_link = asset["binary"]["package"]["link"]
    if not package_link:
        raise ValueError("Adoptium returned no package link.")
    return package_link


def install_jre(
    dest_dir: str,
    major: int,
    progress=None,
    timeout: float = 300.0,
) -> Tuple[str, str]:
    """Download+extract a JRE and return (java_bin, report).

    ``dest_dir`` will contain a per-major subfolder.
    """
    import subprocess

    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, f"jvm{major}")
    java_bin = os.path.join(target, "bin", "java")
    if os.path.exists(java_bin):
        return java_bin, f"JVM {major} already present."

    link = find_latest_jre(major)
    tmp = os.path.join(dest_dir, f"jre{major}.tar.gz")
    download_with_progress(link, tmp, progress=progress, timeout=timeout)
    tmp_extract = tempfile.mkdtemp(prefix="mcsadmin-jre-")
    try:
        with tarfile.open(tmp, "r:gz") as tf:
            tf.extractall(tmp_extract)
        extracted = os.path.join(tmp_extract, os.listdir(tmp_extract)[0])
        if os.path.exists(os.path.join(extracted, "bin", "java")):
            if os.path.exists(target):
                shutil.rmtree(target)
            shutil.move(extracted, target)
        else:
            raise ValueError("JRE archive layout not recognized.")
    finally:
        shutil.rmtree(tmp_extract, ignore_errors=True)
    os.remove(tmp)
    os.chmod(java_bin, 0o755)
    return java_bin, f"Installed Temurin JRE {major}."


def system_java_bin() -> Optional[str]:
    """Best-available Java on PATH."""
    return shutil.which("java")