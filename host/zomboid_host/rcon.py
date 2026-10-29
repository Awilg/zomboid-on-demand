import re
import socket
import struct

AUTH, EXEC, AUTH_RESPONSE = 3, 2, 2


class RconError(Exception):
    pass


class Rcon:
    def __init__(self, host: str, port: int, password: str, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._next_id = 0

    def __enter__(self) -> "Rcon":
        try:
            self._sock = socket.create_connection((self.host, self.port), self.timeout)
            self._sock.settimeout(self.timeout)
            pid = self._send(AUTH, self.password)
            while True:
                rid, rtype, _ = self._recv()
                if rtype == AUTH_RESPONSE:
                    break
            if rid == -1 or rid != pid:
                raise RconError("RCON authentication failed")
        except (OSError, struct.error) as exc:
            self.close()
            raise RconError(f"RCON connection failed: {exc}") from exc
        except RconError:
            self.close()
            raise
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def command(self, cmd: str) -> str:
        if self._sock is None:
            raise RconError("RCON not connected")
        try:
            pid = self._send(EXEC, cmd)
            rid, _, body = self._recv()
        except (OSError, struct.error) as exc:
            raise RconError(f"RCON command failed: {exc}") from exc
        if rid != pid:
            raise RconError("RCON reply id mismatch")
        return body

    def _send(self, ptype: int, body: str) -> int:
        self._next_id += 1
        payload = struct.pack("<ii", self._next_id, ptype) + body.encode() + b"\x00\x00"
        self._sock.sendall(struct.pack("<i", len(payload)) + payload)
        return self._next_id

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed")
            buf += chunk
        return buf

    def _recv(self) -> tuple[int, int, str]:
        (size,) = struct.unpack("<i", self._recv_exact(4))
        data = self._recv_exact(size)
        rid, rtype = struct.unpack("<ii", data[:8])
        return rid, rtype, data[8:-2].decode(errors="replace")


def parse_player_count(players_output: str) -> int:
    first_line = players_output.splitlines()[0] if players_output else ""
    match = re.search(r"\((\d+)\)", first_line)
    if match is None:
        raise RconError(f"cannot find player count in reply: {players_output!r}")
    return int(match.group(1))
