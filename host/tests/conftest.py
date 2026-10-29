import socket
import struct
import threading

import pytest

AUTH, EXEC, RESPONSE = 3, 2, 0


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def _read_packet(conn):
    (size,) = struct.unpack("<i", _recv_exact(conn, 4))
    data = _recv_exact(conn, size)
    pid, ptype = struct.unpack("<ii", data[:8])
    return pid, ptype, data[8:-2].decode()


def _send_packet(conn, pid, ptype, body):
    payload = struct.pack("<ii", pid, ptype) + body.encode() + b"\x00\x00"
    conn.sendall(struct.pack("<i", len(payload)) + payload)


class FakeRcon:
    """Source-RCON server on a local port. Records commands, replies per command."""

    def __init__(self, password="pw"):
        self.password = password
        self.commands: list[str] = []
        self.replies: dict[str, str] = {}
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def set_reply(self, command, reply):
        self.replies[command] = reply

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            try:
                while True:
                    pid, ptype, body = _read_packet(conn)
                    if ptype == AUTH:
                        _send_packet(conn, pid, RESPONSE, "")
                        ok = body == self.password
                        _send_packet(conn, pid if ok else -1, EXEC, "")
                    elif ptype == EXEC:
                        self.commands.append(body)
                        _send_packet(conn, pid, RESPONSE, self.replies.get(body, ""))
            except (ConnectionError, OSError):
                return

    def close(self):
        self._sock.close()


@pytest.fixture
def fake_rcon():
    server = FakeRcon()
    server.set_reply("players", "Players connected (2):\n-a\n-b")
    yield server
    server.close()


REGION = "us-west-2"


@pytest.fixture
def s3():
    import boto3
    from moto import mock_aws

    with mock_aws():
        yield boto3.client("s3", region_name=REGION)


@pytest.fixture
def bucket(s3):
    s3.create_bucket(
        Bucket="zomboid-test",
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    return "zomboid-test"


@pytest.fixture
def data_dir(tmp_path):
    """Fake Zomboid data dir with one file in each archived location."""
    root = tmp_path / "data"
    (root / "Saves/Multiplayer/pz").mkdir(parents=True)
    (root / "Saves/Multiplayer/pz/map.bin").write_text("ORIGINAL")
    (root / "db").mkdir()
    (root / "db/pz.db").write_text("DB")
    (root / "Server").mkdir()
    (root / "Server/pz.ini").write_text("INI")
    return root
