from datetime import datetime, timedelta, timezone

import boto3
from moto import mock_aws

from zomboid_host.settings import Settings
from zomboid_host.status import read_status, write_status
from zomboid_host.watchdog import Action, WatchState, decide, load_state, save_state

t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
s = Settings(server_name="x")


def m(n):
    return timedelta(minutes=n)


def test_no_action_when_players_online():
    st = WatchState(t0, t0, set())
    assert decide(t0 + m(60), 3, st, s) == []


def test_idle_shutdown_after_30_minutes():
    st = WatchState(t0, t0, set())
    assert decide(t0 + m(29), 0, st, s) == []
    assert decide(t0 + m(30), 0, st, s) == [Action("shutdown", reason="idle")]


def test_player_resets_idle_clock():
    st = WatchState(t0, t0, set())
    decide(t0 + m(25), 1, st, s)
    assert decide(t0 + m(50), 0, st, s) == []


def test_hard_limit_warnings_once_each():
    st = WatchState(t0, t0, set())
    acts = decide(t0 + m(465), 4, st, s)
    assert Action("warn_game", minutes_left=15) in acts and Action("warn_discord", minutes_left=15) in acts
    assert decide(t0 + m(466), 4, st, s) == []
    assert decide(t0 + m(475), 4, st, s) == [Action("warn_game", minutes_left=5)]


def test_hard_limit_shutdown_with_players_online():
    st = WatchState(t0, t0, {15, 5})
    assert decide(t0 + m(480), 6, st, s) == [Action("shutdown", reason="hard_limit")]


def test_rcon_unreachable_counts_as_idle():
    st = WatchState(t0, t0, set())
    assert decide(t0 + m(30), None, st, s) == [Action("shutdown", reason="idle")]


def test_rcon_unreachable_still_hits_hard_limit():
    st = WatchState(t0, t0 + m(479), {15, 5})
    assert decide(t0 + m(480), None, st, s) == [Action("shutdown", reason="hard_limit")]


def test_state_json_round_trip(tmp_path):
    p = tmp_path / "watch.json"
    st = WatchState(t0, t0 + m(5), {15})
    save_state(p, st)
    assert load_state(p) == st


@mock_aws
def test_status_round_trip():
    ssm = boto3.client("ssm", region_name="us-east-1")
    assert read_status(ssm) == {"state": "stopped"}
    write_status(ssm, "running", 2, "1.2.3.4:16261", t0)
    got = read_status(ssm)
    assert got["state"] == "running" and got["players"] == 2
    assert got["address"] == "1.2.3.4:16261"
    assert got["session_start"] == t0.isoformat()
    assert datetime.fromisoformat(got["updated_at"]).utcoffset() == timedelta(0)
    write_status(ssm, "stopped", None, None, None)
    got = read_status(ssm)
    assert got["state"] == "stopped" and got["address"] is None and got["session_start"] is None


def test_save_state_replaces_file_atomically(tmp_path):
    p = tmp_path / "watch.json"
    p.write_text("old")
    save_state(p, WatchState(t0, t0, {5}))
    assert load_state(p) == WatchState(t0, t0, {5})
    assert [f.name for f in tmp_path.iterdir()] == ["watch.json"]
