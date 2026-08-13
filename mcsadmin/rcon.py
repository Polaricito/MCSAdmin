"""Minimal Minecraft RCON client (source RCON protocol).

RCON uses a 4-byte little-endian length header followed by a 12-byte
payload (id + type + body + two null bytes). Login is type 3, commands
are type 2.
"""

from __future__ import annotations

import socket
import struct
import time
from typing import List, Optional

PACKET_LOGIN = 3
PACKET_COMMAND = 2
PACKET_RESPONSE = 0

MAX_PAYLOAD = 4096 - 14


class RCONError(Exception):
    pass


class RCONClient:
    """A simple persistent RCON connection."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 25575,
        password: str = "",
        timeout: float = 3.0,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._id = 0

    # ------------------------------------------------------------------
    def _connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock

    def _send_packet(self, pkt_type: int, body: str) -> int:
        if self._sock is None:
            raise RCONError("Not connected.")
        self._id = (self._id + 1) & 0x7FFFFFFF
        ident = self._id
        payload = body.encode("utf-8")[:MAX_PAYLOAD]
        body_bin = payload + b"\x00\x00"
        # Length covers request id (4) + type (4) + payload + trailing nulls (2).
        length = 4 + 4 + len(body_bin)
        if length > 4096:
            raise RCONError("Packet too large.")
        header = struct.pack("<ii", length, ident)
        data = header + struct.pack("<i", pkt_type) + body_bin
        self._sock.sendall(data)
        return ident

    def _recv_packet(self) -> Optional[tuple]:
        if self._sock is None:
            return None
        header = b""
        while len(header) < 4:
            chunk = self._sock.recv(4 - len(header))
            if not chunk:
                raise RCONError("Connection closed.")
            header += chunk
        (length,) = struct.unpack("<i", header)
        if length <= 0 or length > 8192:
            raise RCONError(f"Bad packet length {length}.")
        data = b""
        while len(data) < length:
            chunk = self._sock.recv(length - len(data))
            if not chunk:
                raise RCONError("Connection closed.")
            data += chunk
        ident, pkt_type = struct.unpack("<ii", data[:8])
        body = data[8:]  # includes trailing nulls
        return ident, pkt_type, body

    # ------------------------------------------------------------------
    def login(self) -> None:
        self.close()
        self._connect()
        ident = self._send_packet(PACKET_LOGIN, self.password)
        while True:
            resp = self._recv_packet()
            if resp is None:
                break
            rid, rtype, _body = resp
            if rtype == PACKET_RESPONSE and rid == ident:
                if rid == -1:
                    raise RCONError("Login rejected (bad password).")
                return
            # Response to login may carry the same id; treat as success.
            if rid == ident:
                return

    def command(self, cmd: str) -> str:
        """Send a command and return its response body."""
        if self._sock is None:
            self.login()
        ident = self._send_packet(PACKET_COMMAND, cmd)
        responses = []
        deadline = time.time() + self.timeout * 2
        while time.time() < deadline:
            resp = self._recv_packet()
            if resp is None:
                break
            rid, rtype, body = resp
            if rtype == PACKET_RESPONSE and rid == ident:
                text = body.rstrip(b"\x00").decode("utf-8", "replace")
                responses.append(text)
                # In practice the server may split long responses into
                # multiple packets; keep draining briefly.
                break
        return "".join(responses)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def parse_player_list(raw: str) -> List[str]:
    """Parse the output of the 'list' command.

    Expected format:
        There are 3 of a max of 20 players online: Alice, Bob, Carol
    The RCON response is usually prefixed with the raw chat-style line.
    """
    text = raw.split("]:")[-1] if "]:" in raw else raw
    players: List[str] = []
    if "players online" in text.lower():
        marker = text.lower().find("online")
        after = text[marker + len("online"):]
        if ":" in after:
            names = after.split(":", 1)[1].strip()
            if names:
                players = [n.strip() for n in names.split(",") if n.strip()]
    # GPT-style: some versions simply return "No players online." etc.
    clean = [p for p in players if p and not p.startswith("There")]
    return clean