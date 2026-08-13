"""Minecraft server icon helper (pure standard library).

The vanilla server only accepts ``server-icon.png`` as an exactly 64x64
8-bit RGBA PNG; any other size/format is silently ignored by the client,
which is why a picked icon can look like it "isn't loading". This module
decodes a PNG (via zlib), resizes it to 64x64 with nearest-neighbour
sampling and re-encodes it, so any PNG the user points at — or pastes as a
path — becomes a valid icon. JPEGs are converted when ImageMagick is
available.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import zlib
from typing import List, Optional, Tuple

ICON_SIZE = 64
_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _decode_png(data: bytes) -> Tuple[int, int, int, bytes, bytes]:
    """Return (width, height, color_type, plte, unfiltered scanlines)."""
    if not data.startswith(_PNG_SIG):
        raise ValueError("not a PNG file")
    pos = 8
    width = height = depth = color = None
    interlace = 0
    plte = b""
    idat = b""
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if typ == b"IHDR":
            width, height, depth, color, _comp, _filter, interlace = (
                struct.unpack(">IIBBBBB", body[:13])
            )
        elif typ == b"PLTE":
            plte = body
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        pos += 12 + length
    if None in (width, height, depth, color):
        raise ValueError("incomplete PNG header")
    if depth != 8:
        raise ValueError("only 8-bit PNG icons are supported")
    if color not in (0, 2, 3, 4, 6):
        raise ValueError(f"unsupported PNG colour type {color}")
    if interlace != 0:
        raise ValueError("interlaced PNG icons are not supported")

    raw = zlib.decompress(idat)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    stride = width * channels
    out = bytearray()
    prev = bytearray(stride)
    pos2 = 0
    for _ in range(height):
        if pos2 >= len(raw):
            raise ValueError("truncated PNG data")
        f = raw[pos2]
        pos2 += 1
        row = bytearray(raw[pos2:pos2 + stride])
        pos2 += stride
        if f == 1:
            for i in range(channels, stride):
                row[i] = (row[i] + row[i - channels]) & 0xFF
        elif f == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                a = row[i - channels] if i >= channels else 0
                row[i] = (row[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = row[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                row[i] = (row[i] + pr) & 0xFF
        elif f != 0:
            raise ValueError(f"unknown PNG scanline filter {f}")
        out += row
        prev = row
    return width, height, color, plte, bytes(out)


def _pixel(buf: bytes, width: int, color: int, plte: bytes, x: int, y: int) -> Tuple[int, int, int, int]:
    if color in (2, 6):
        ch = 3 if color == 2 else 4
        i = (y * width + x) * ch
        r, g, b = buf[i], buf[i + 1], buf[i + 2]
        a = buf[i + 3] if color == 6 else 255
        return r, g, b, a
    if color in (0, 4):
        ch = 1 if color == 0 else 2
        i = (y * width + x) * ch
        v = buf[i]
        a = buf[i + 1] if color == 4 else 255
        return v, v, v, a
    i = y * width + x
    o = buf[i] * 3
    return plte[o], plte[o + 1], plte[o + 2], 255


def _to_icon_pixels(buf: bytes, w: int, h: int, color: int, plte: bytes) -> List[Tuple[int, int, int, int]]:
    out: List[Tuple[int, int, int, int]] = []
    for y in range(ICON_SIZE):
        sy = y * h // ICON_SIZE
        sy = min(sy, h - 1)
        for x in range(ICON_SIZE):
            sx = x * w // ICON_SIZE
            sx = min(sx, w - 1)
            out.append(_pixel(buf, w, color, plte, sx, sy))
    return out


def _encode_png(size: int, pixels: List[Tuple[int, int, int, int]]) -> bytes:
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            r, g, b, a = pixels[y * size + x]
            rows += bytes((r, g, b, a))
    raw = zlib.compress(bytes(rows), 9)

    def chunk(typ: bytes, body: bytes) -> bytes:
        blob = typ + body
        return (
            struct.pack(">I", len(body))
            + blob
            + struct.pack(">I", zlib.crc32(blob) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        _PNG_SIG
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", raw)
        + chunk(b"IEND", b"")
    )


def _via_imagemagick(src: str, dst: str) -> Optional[str]:
    """Convert any image via ImageMagick if installed."""
    for tool in ("magick", "convert"):
        if shutil.which(tool):
            try:
                subprocess.run(
                    [tool, src, "-resize", f"{ICON_SIZE}x{ICON_SIZE}!",
                     "-define", "png:color-type=6", dst],
                    check=True, capture_output=True, timeout=60,
                )
                return None
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                return None
    return None


def normalize_icon(src: str, dst: str) -> Optional[str]:
    """Write a valid 64x64 server-icon.png for ``src``.

    Returns an error message on failure, or ``None`` on success.
    """
    try:
        with open(src, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return f"could not read {src}: {exc}"

    if src.lower().endswith((".jpg", ".jpeg")):
        if os.path.abspath(src) == os.path.abspath(dst):
            return "cannot convert in place"
        err = _via_imagemagick(src, dst)
        return err or None

    try:
        w, h, color, plte, buf = _decode_png(data)
    except Exception as exc:  # noqa: BLE001
        return str(exc)

    if w == h == ICON_SIZE and color == 6:
        # already a valid icon: copy verbatim
        if os.path.abspath(src) != os.path.abspath(dst):
            try:
                with open(dst, "wb") as fh:
                    fh.write(data)
            except OSError as exc:
                return f"could not write {dst}: {exc}"
        return None

    pixels = _to_icon_pixels(buf, w, h, color, plte)
    try:
        with open(dst, "wb") as fh:
            fh.write(_encode_png(ICON_SIZE, pixels))
    except OSError as exc:
        return f"could not write {dst}: {exc}"
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None