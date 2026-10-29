import pytest

from zomboid_host.rcon import Rcon, RconError, parse_player_count


def test_command_round_trip(fake_rcon):
    with Rcon("127.0.0.1", fake_rcon.port, "pw") as r:
        assert parse_player_count(r.command("players")) == 2
    assert fake_rcon.commands == ["players"]


def test_bad_password_raises(fake_rcon):
    with pytest.raises(RconError):
        Rcon("127.0.0.1", fake_rcon.port, "wrong").__enter__()


def test_connect_refused_raises():
    with pytest.raises(RconError):
        Rcon("127.0.0.1", 1, "pw", timeout=0.5).__enter__()


def test_command_on_closed_socket_raises(fake_rcon):
    fake_rcon.set_reply("players", "x")
    r = Rcon("127.0.0.1", fake_rcon.port, "pw", timeout=0.3)
    r.__enter__()
    r._sock.close()
    with pytest.raises(RconError):
        r.command("players")


def test_parse_player_count_zero():
    assert parse_player_count("Players connected (0): ") == 0


def test_parse_player_count_no_count_raises():
    with pytest.raises(RconError):
        parse_player_count("unexpected reply")
    with pytest.raises(RconError):
        parse_player_count("")
