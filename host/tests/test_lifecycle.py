import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import boto3
import pytest

from pathlib import Path

from zomboid_host import cli, lifecycle, notify, watchdog
from zomboid_host.bundle import fetch_bundle
from zomboid_host.backup import list_backups, make_archive
from zomboid_host.lifecycle import HostContext, Paths, boot, shutdown
from zomboid_host.rcon import RconError
from zomboid_host.settings import Settings
from zomboid_host.status import read_status
from zomboid_host.watchdog import WatchState, load_state, run_once, save_state

T0 = datetime(2026, 10, 27, 18, 0, tzinfo=UTC)
IP = "203.0.113.7"
REGION = "us-west-2"
REAL_POST = notify.post


class FakeClock:
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


class FakeRun:
    """Records commands. `systemctl is-active` follows `is_active()`."""

    def __init__(self, is_active):
        self.calls: list[list[str]] = []
        self.is_active = is_active
        self.on_call = None

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if self.on_call:
            self.on_call(list(cmd))
        if cmd[:2] == ["systemctl", "is-active"]:
            return 0 if self.is_active() else 3
        return 0


def rcon_down(ctx):
    raise RconError("connection refused")


def marker_path(ctx):
    return ctx.paths.marker


@pytest.fixture(autouse=True)
def posts(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "post", lambda url, content: sent.append(content))
    return sent


@pytest.fixture
def ssm(s3):
    client = boto3.client("ssm", region_name=REGION)
    for name, value in [
        ("/zomboid/game_password", "gamepw"),
        ("/zomboid/rcon_password", "pw"),
        ("/zomboid/discord_webhook_url", "https://discord.test/hook"),
    ]:
        client.put_parameter(Name=name, Value=value, Type="SecureString")
    client.put_parameter(Name="/zomboid/restore_key", Value="none", Type="String")
    return client


@pytest.fixture
def clock():
    return FakeClock(T0)


@pytest.fixture
def ctx(tmp_path, s3, ssm, bucket, data_dir, fake_rcon, clock):
    run = FakeRun(is_active=lambda: "quit" not in fake_rcon.commands)
    c = HostContext(
        settings=Settings(server_name="pz", rcon_port=fake_rcon.port),
        s3=s3,
        ssm=ssm,
        bucket=bucket,
        webhook_url="https://discord.test/hook",
        rcon_password="pw",
        game_password="gamepw",
        run=run,
        paths=Paths(tmp_path),
        now=clock.now,
        sleep=clock.sleep,
        public_ip=lambda: IP,
        boot_time=lambda: T0 - timedelta(minutes=5),
    )
    c.calls = run.calls
    c.fake_run = run
    return c


@pytest.fixture
def ctx_game_never_exits(ctx):
    ctx.fake_run.is_active = lambda: True
    return ctx


@pytest.fixture
def ctx_s3_down(ctx):
    ctx.bucket = "no-such-bucket"
    return ctx


# --- shutdown ---------------------------------------------------------------


def test_shutdown_order(ctx, fake_rcon, posts, clock):
    shutdown(ctx, "idle")
    assert fake_rcon.commands == ["save", "quit"]
    assert ctx.calls[0] == ["systemctl", "stop", "zomboid-watchdog.timer"]
    assert ctx.calls[-1] == ["systemctl", "poweroff"]
    assert ["systemctl", "kill", "-s", "KILL", "zomboid-game"] not in ctx.calls
    assert list_backups(ctx.s3, ctx.bucket)[0].endswith("-shutdown.tar.zst")
    assert marker_path(ctx).exists()
    assert read_status(ctx.ssm)["state"] == "stopped"
    assert posts == ["The server stopped (no players for 30 minutes). The world is saved."]
    assert clock.t - T0 >= timedelta(seconds=10)


def test_shutdown_kills_hung_game_and_still_powers_off(ctx_game_never_exits, clock):
    shutdown(ctx_game_never_exits, "idle")
    assert ["systemctl", "kill", "-s", "KILL", "zomboid-game"] in ctx_game_never_exits.calls
    assert ctx_game_never_exits.calls[-1] == ["systemctl", "poweroff"]
    assert not marker_path(ctx_game_never_exits).exists()
    assert clock.t - T0 >= timedelta(seconds=310)


def test_shutdown_skips_when_another_shutdown_holds_the_lock(ctx, fake_rcon, posts):
    import fcntl

    ctx.paths.state.mkdir(parents=True, exist_ok=True)
    with open(ctx.paths.state / "shutdown.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        shutdown(ctx, "idle")
    assert fake_rcon.commands == []
    assert ctx.calls == []
    assert posts == []
    assert list_backups(ctx.s3, ctx.bucket) == []


def test_shutdown_powers_off_when_backup_fails(ctx_s3_down, posts):
    shutdown(ctx_s3_down, "idle")
    assert ctx_s3_down.calls[-1] == ["systemctl", "poweroff"]
    assert any("back-up failed" in p for p in posts)
    assert posts[0] == "The back-up failed. The world is still on the server disk."


def test_shutdown_with_rcon_down_still_backs_up_and_powers_off(ctx, fake_rcon):
    ctx.fake_run.is_active = lambda: False
    ctx.rcon_factory = rcon_down
    shutdown(ctx, "admin")
    assert fake_rcon.commands == []
    assert list_backups(ctx.s3, ctx.bucket)[0].endswith("-shutdown.tar.zst")
    assert not marker_path(ctx).exists()
    assert ctx.calls[-1] == ["systemctl", "poweroff"]


def test_shutdown_powers_off_when_discord_post_raises(ctx, monkeypatch):
    def broken(url, content):
        raise RuntimeError("boom")

    monkeypatch.setattr(notify, "post", broken)
    with pytest.raises(RuntimeError):
        shutdown(ctx, "idle")
    assert ctx.calls[-1] == ["systemctl", "poweroff"]


@pytest.mark.parametrize(
    "reason, text",
    [
        ("hard_limit", "8-hour limit"),
        ("admin", "stopped by an admin"),
        ("boot_failed", "start-up failed"),
        ("crash", "the game stopped unexpectedly"),
    ],
)
def test_shutdown_stopped_message_text(ctx, posts, reason, text):
    shutdown(ctx, reason)
    assert posts[-1] == f"The server stopped ({text}). The world is saved."


# --- boot -------------------------------------------------------------------


def _upload_bundle(s3, bucket, rcon_port, extra_settings=""):
    files = {
        "config/settings.toml": f'server_name = "pz"\nrcon_port = {rcon_port}\n{extra_settings}',
        "config/server/pz.ini": "Public=true\nPassword=\nRCONPassword=\n",
        "config/server/pz_SandboxVars.lua": "SandboxVars = {}\n",
        "host/systemd/zomboid-game.service": "[Service]\n",
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    s3.put_object(Bucket=bucket, Key="bundle/current.tar.gz", Body=buf.getvalue())


@pytest.fixture
def boot_ctx(ctx, fake_rcon):
    _upload_bundle(ctx.s3, ctx.bucket, fake_rcon.port)
    ctx.paths.server.mkdir(parents=True)
    (ctx.paths.server / "ProjectZomboid64.json").write_text(
        json.dumps({"mainClass": "zombie/network/GameServer", "vmArgs": ["-Xms1g", "-Xmx8g", "-Dx=1"]})
    )
    ctx.settings = None
    ctx.webhook_url = ctx.rcon_password = ctx.game_password = ""
    return ctx


def _steamcmd_calls(ctx):
    return [c for c in ctx.calls if c[0].endswith("steamcmd")]


def test_boot_restores_requested_backup_then_clears_param(boot_ctx, tmp_path, posts):
    other = tmp_path / "other"
    (other / "Saves/Multiplayer/pz").mkdir(parents=True)
    (other / "Saves/Multiplayer/pz/map.bin").write_text("BACKUP")
    (other / "db").mkdir()
    (other / "db/pz.db").write_text("OLDDB")
    key = "backups/20261001T000000Z-manual.tar.zst"
    make_archive(other, "pz", tmp_path / "b.tar.zst")
    boot_ctx.s3.upload_file(str(tmp_path / "b.tar.zst"), boot_ctx.bucket, key)
    boot_ctx.ssm.put_parameter(Name="/zomboid/restore_key", Value=key, Type="String", Overwrite=True)

    boot(boot_ctx)

    data = boot_ctx.paths.data
    assert (data / "Saves/Multiplayer/pz/map.bin").read_text() == "BACKUP"
    assert boot_ctx.ssm.get_parameter(Name="/zomboid/restore_key")["Parameter"]["Value"] == "none"
    assert f"Restored back-up {key}." in posts
    assert list_backups(boot_ctx.s3, boot_ctx.bucket) == [key]


def _upload_backup(ctx, tmp_path, key, text="BACKUP"):
    other = tmp_path / "other"
    (other / "Saves/Multiplayer/pz").mkdir(parents=True)
    (other / "Saves/Multiplayer/pz/map.bin").write_text(text)
    (other / "db").mkdir()
    (other / "db/pz.db").write_text("OLDDB")
    make_archive(other, "pz", tmp_path / "b.tar.zst")
    ctx.s3.upload_file(str(tmp_path / "b.tar.zst"), ctx.bucket, key)


def test_boot_restores_newest_backup_when_no_world_on_disk(boot_ctx, tmp_path, posts):
    shutil.rmtree(boot_ctx.paths.data)
    boot_ctx.paths.data.mkdir()
    key = "backups/20261001T000000Z-shutdown.tar.zst"
    _upload_backup(boot_ctx, tmp_path, key)

    boot(boot_ctx)

    assert (boot_ctx.paths.data / "Saves/Multiplayer/pz/map.bin").read_text() == "BACKUP"
    assert posts[0] == f"Restored back-up {key}."
    assert posts[1].startswith("The server is ready.")
    assert list_backups(boot_ctx.s3, boot_ctx.bucket) == [key]


def test_boot_starts_fresh_world_when_no_world_and_no_backups(boot_ctx, posts):
    shutil.rmtree(boot_ctx.paths.data)
    boot_ctx.paths.data.mkdir()
    boot(boot_ctx)
    assert not (boot_ctx.paths.data / "Saves/Multiplayer/pz").exists()
    assert len(posts) == 1 and posts[0].startswith("The server is ready.")
    assert list_backups(boot_ctx.s3, boot_ctx.bucket) == []


def test_boot_does_not_auto_restore_when_world_exists(boot_ctx, tmp_path, posts):
    marker_path(boot_ctx).parent.mkdir(parents=True, exist_ok=True)
    marker_path(boot_ctx).write_text("")
    _upload_backup(boot_ctx, tmp_path, "backups/20261001T000000Z-shutdown.tar.zst")
    boot(boot_ctx)
    assert (boot_ctx.paths.data / "Saves/Multiplayer/pz/map.bin").read_text() == "ORIGINAL"
    assert not any(p.startswith("Restored") for p in posts)


def test_boot_failed_restore_names_the_key(boot_ctx, posts):
    key = "backups/20261001T000000Z-missing.tar.zst"
    boot_ctx.ssm.put_parameter(Name="/zomboid/restore_key", Value=key, Type="String", Overwrite=True)
    boot(boot_ctx)
    assert posts[0] == (
        f"The server did not start. The restore of back-up {key} failed. The server will shut down."
    )
    assert not _steamcmd_calls(boot_ctx)
    assert boot_ctx.calls[-1] == ["systemctl", "poweroff"]
    assert boot_ctx.ssm.get_parameter(Name="/zomboid/restore_key")["Parameter"]["Value"] == key


def test_boot_backs_up_before_update_after_unclean_stop(boot_ctx):
    seen = []

    def on_call(cmd):
        if cmd[0].endswith("steamcmd"):
            seen.append(list_backups(boot_ctx.s3, boot_ctx.bucket))

    boot_ctx.fake_run.on_call = on_call
    boot(boot_ctx)
    assert len(seen) == 1
    assert len(seen[0]) == 1 and seen[0][0].endswith("-pre-update.tar.zst")


def test_boot_skips_preupdate_backup_after_clean_stop(boot_ctx):
    marker_path(boot_ctx).parent.mkdir(parents=True, exist_ok=True)
    marker_path(boot_ctx).write_text("")
    boot(boot_ctx)
    assert list_backups(boot_ctx.s3, boot_ctx.bucket) == []
    assert not marker_path(boot_ctx).exists()


def test_boot_posts_ready_with_address_and_password(boot_ctx, posts, fake_rcon):
    boot(boot_ctx)
    assert posts == [
        "The server is ready.\n"
        f"Address: {IP}:16261\n"
        "Password: gamepw\n"
        "The address changes at each start. In the game, edit your saved server and change only the IP.\n"
        "The server stops after 30 minutes with no players, or after 8 hours."
    ]
    status = read_status(boot_ctx.ssm)
    assert status["state"] == "running"
    assert status["address"] == f"{IP}:16261"
    calls = boot_ctx.calls
    assert calls[-1] == ["systemctl", "start", "zomboid-watchdog.timer"]
    assert calls.index(_steamcmd_calls(boot_ctx)[0]) < calls.index(["systemctl", "start", "zomboid-game"])


def test_boot_runs_steamcmd_on_public_branch(boot_ctx):
    boot(boot_ctx)
    assert _steamcmd_calls(boot_ctx) == [
        [
            "/usr/games/steamcmd",
            "+force_install_dir", str(boot_ctx.paths.server),
            "+login", "anonymous",
            "+app_update", "380870", "validate",
            "+quit",
        ]
    ]


def test_boot_schedules_failsafe_poweroff_first(boot_ctx):
    boot(boot_ctx)
    assert boot_ctx.calls[0] == ["shutdown", "-P", "+500"]
    assert [c for c in boot_ctx.calls if c[0] == "shutdown"] == [["shutdown", "-P", "+500"]] * 2


def test_boot_reschedules_failsafe_after_steamcmd(boot_ctx):
    boot(boot_ctx)
    calls = boot_ctx.calls
    steamcmd = calls.index(_steamcmd_calls(boot_ctx)[0])
    assert calls[steamcmd + 1] == ["shutdown", "-P", "+500"]
    assert steamcmd + 1 < calls.index(["systemctl", "start", "zomboid-game"])


def test_boot_reschedules_failsafe_from_settings(boot_ctx, fake_rcon):
    _upload_bundle(boot_ctx.s3, boot_ctx.bucket, fake_rcon.port, "hard_limit_minutes = 60\n")
    boot(boot_ctx)
    assert [c for c in boot_ctx.calls if c[0] == "shutdown"] == [
        ["shutdown", "-P", "+500"],
        ["shutdown", "-P", "+80"],
        ["shutdown", "-P", "+80"],
    ]


def test_boot_powers_off_when_bundle_is_missing(boot_ctx, posts):
    boot_ctx.s3.delete_object(Bucket=boot_ctx.bucket, Key="bundle/current.tar.gz")
    boot(boot_ctx)
    assert boot_ctx.calls[0] == ["shutdown", "-P", "+500"]
    assert boot_ctx.calls[-1] == ["systemctl", "poweroff"]
    assert not _steamcmd_calls(boot_ctx)
    assert posts == []


def test_boot_stays_on_when_bundle_is_missing_and_hold_exists(boot_ctx):
    boot_ctx.paths.hold.parent.mkdir(parents=True, exist_ok=True)
    boot_ctx.paths.hold.write_text("")
    boot_ctx.s3.delete_object(Bucket=boot_ctx.bucket, Key="bundle/current.tar.gz")
    boot(boot_ctx)
    assert ["systemctl", "poweroff"] not in boot_ctx.calls


@pytest.mark.parametrize("param", ["/zomboid/game_password", "/zomboid/rcon_password"])
def test_boot_fails_on_empty_password(boot_ctx, posts, param):
    boot_ctx.ssm.put_parameter(Name=param, Value=" ", Type="SecureString", Overwrite=True)
    boot(boot_ctx)
    assert posts == ["The server did not start. The server will shut down."]
    assert boot_ctx.calls[-1] == ["systemctl", "poweroff"]
    assert not _steamcmd_calls(boot_ctx)
    assert ["systemctl", "start", "zomboid-game"] not in boot_ctx.calls


def test_boot_skip_fetch_uses_bundle_on_disk(boot_ctx, posts):
    fetch_bundle(boot_ctx.s3, boot_ctx.bucket, boot_ctx.paths.bundle)
    boot_ctx.s3.delete_object(Bucket=boot_ctx.bucket, Key="bundle/current.tar.gz")
    boot(boot_ctx, fetch=False)
    assert posts[0].startswith("The server is ready.")


def test_boot_installs_repo_config_over_restored_config(boot_ctx, fake_rcon):
    boot(boot_ctx)
    server = boot_ctx.paths.data / "Server"
    ini = (server / "pz.ini").read_text().splitlines()
    assert "Public=true" in ini
    assert "Password=gamepw" in ini
    assert "RCONPassword=pw" in ini
    assert f"RCONPort={fake_rcon.port}" in ini
    assert "SaveWorldEveryMinutes=10" in ini
    assert "DefaultPort=16261" in ini
    assert (server / "pz_SandboxVars.lua").read_text() == "SandboxVars = {}\n"


def test_boot_sets_xmx_env_and_watch_state(boot_ctx, clock):
    boot(boot_ctx)
    vm = json.loads((boot_ctx.paths.server / "ProjectZomboid64.json").read_text())["vmArgs"]
    assert vm == ["-Xms1g", "-Xmx6g", "-Dx=1"]
    env = boot_ctx.paths.game_env
    assert env.read_text() == 'SERVER_NAME=pz\nADMIN_PASSWORD="pw"\n'
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    st = load_state(boot_ctx.paths.watch)
    assert st.session_start == st.last_player_seen == T0


def test_boot_gives_up_and_shuts_down_when_rcon_never_answers(boot_ctx, posts, clock):
    boot_ctx.rcon_factory = rcon_down
    boot_ctx.fake_run.is_active = lambda: False
    boot(boot_ctx)
    assert posts[0] == "The server did not start. The server will shut down."
    assert posts[-1] == "The server stopped (start-up failed). The world is saved."
    assert boot_ctx.calls[-1] == ["systemctl", "poweroff"]
    assert ["systemctl", "start", "zomboid-watchdog.timer"] not in boot_ctx.calls
    assert clock.t - T0 >= timedelta(minutes=15)


def test_boot_failure_with_hold_posts_but_stays_on(boot_ctx, posts, fake_rcon):
    boot_ctx.paths.hold.parent.mkdir(parents=True, exist_ok=True)
    boot_ctx.paths.hold.write_text("")
    boot_ctx.rcon_factory = rcon_down
    boot(boot_ctx)
    assert posts == ["The server did not start. The server will shut down."]
    assert ["systemctl", "poweroff"] not in boot_ctx.calls
    assert fake_rcon.commands == []


# --- watchdog run_once ------------------------------------------------------


@pytest.fixture
def stopped_with(monkeypatch):
    reasons = []
    monkeypatch.setattr(lifecycle, "shutdown", lambda ctx, reason: reasons.append(reason))
    return reasons


@pytest.fixture
def fake_rcon_zero_players(fake_rcon):
    fake_rcon.set_reply("players", "Players connected (0):")
    return fake_rcon


def test_run_once_shuts_down_after_idle(ctx, fake_rcon_zero_players, stopped_with):
    save_state(ctx.paths.watch, WatchState(T0 - timedelta(minutes=60), T0 - timedelta(minutes=30), set()))
    run_once(ctx)
    assert stopped_with == ["idle"]


def test_run_once_writes_state_and_status(ctx, fake_rcon, stopped_with):
    save_state(ctx.paths.watch, WatchState(T0 - timedelta(minutes=60), T0 - timedelta(minutes=50), set()))
    run_once(ctx)
    assert stopped_with == []
    assert load_state(ctx.paths.watch).last_player_seen == T0
    status = read_status(ctx.ssm)
    assert status["state"] == "running"
    assert status["players"] == 2
    assert status["address"] == f"{IP}:16261"
    assert status["session_start"] == (T0 - timedelta(minutes=60)).isoformat()


def test_run_once_sends_warnings(ctx, fake_rcon, posts, stopped_with):
    save_state(ctx.paths.watch, WatchState(T0 - timedelta(minutes=466), T0, set()))
    run_once(ctx)
    assert 'servermsg "Server restart in 15 minutes. The world is saved."' in fake_rcon.commands
    assert posts == ["The server stops in 15 minutes (8-hour limit). Type /start after the stop to continue."]
    assert load_state(ctx.paths.watch).warned == {15}


@pytest.mark.parametrize("content", [None, "{not json", '{"session_start": 1}'])
def test_run_once_uses_boot_time_when_watch_state_missing_or_corrupt(ctx, fake_rcon_zero_players, stopped_with, content):
    if content is not None:
        ctx.paths.watch.parent.mkdir(parents=True, exist_ok=True)
        ctx.paths.watch.write_text(content)
    booted = T0 - timedelta(hours=8)
    ctx.boot_time = lambda: booted
    run_once(ctx)
    assert stopped_with == ["hard_limit"]
    assert load_state(ctx.paths.watch).session_start == booted


def test_run_once_shuts_down_at_once_when_game_stopped(ctx, fake_rcon, stopped_with):
    ctx.fake_run.is_active = lambda: False
    save_state(ctx.paths.watch, WatchState(T0 - timedelta(minutes=60), T0, set()))
    run_once(ctx)
    assert stopped_with == ["crash"]
    assert ["systemctl", "is-active", "--quiet", "zomboid-game"] in ctx.calls
    assert fake_rcon.commands == []


def test_run_once_does_not_stop_when_game_active(ctx, fake_rcon, stopped_with):
    save_state(ctx.paths.watch, WatchState(T0 - timedelta(minutes=60), T0, set()))
    run_once(ctx)
    assert stopped_with == []
    assert ["systemctl", "is-active", "--quiet", "zomboid-game"] in ctx.calls


def test_run_once_counts_rcon_down_as_no_players(ctx, stopped_with):
    ctx.rcon_factory = rcon_down
    save_state(ctx.paths.watch, WatchState(T0 - timedelta(minutes=60), T0 - timedelta(minutes=30), set()))
    run_once(ctx)
    assert stopped_with == ["idle"]
    assert read_status(ctx.ssm)["players"] is None


# --- notify -----------------------------------------------------------------


def test_notify_posts_json_content():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.headers["Content-Type"], json.loads(body)))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    try:
        REAL_POST(f"http://127.0.0.1:{server.server_port}/hook", "hello")
    finally:
        server.server_close()
    assert received == [("application/json", {"content": "hello"})]


def test_notify_swallows_errors(caplog):
    REAL_POST("http://127.0.0.1:1/hook", "hello")
    assert "Discord post failed" in caplog.text


# --- cli --------------------------------------------------------------------


def test_cli_dispatches_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "load_context", lambda: "CTX")
    monkeypatch.setattr(lifecycle, "shutdown", lambda ctx, reason: calls.append(("shutdown", reason)))
    monkeypatch.setattr(lifecycle, "backup", lambda ctx, reason: calls.append(("backup", reason)) or "key")
    monkeypatch.setattr(watchdog, "run_once", lambda ctx: calls.append(("watchdog",)))
    cli.main(["shutdown"])
    cli.main(["shutdown", "--reason", "idle"])
    cli.main(["backup"])
    cli.main(["watchdog"])
    assert calls == [("shutdown", "admin"), ("shutdown", "idle"), ("backup", "manual"), ("watchdog",)]


@pytest.fixture
def cli_runs(monkeypatch, tmp_path):
    runs = []
    monkeypatch.setattr(lifecycle, "run_command", lambda cmd: runs.append(cmd) or 0)
    monkeypatch.setattr(cli, "PATHS", Paths(tmp_path))
    return runs


def _fail():
    raise FileNotFoundError("host.env")


def test_cli_shutdown_powers_off_when_context_fails(monkeypatch, cli_runs):
    monkeypatch.setattr(cli, "load_context", _fail)
    with pytest.raises(SystemExit):
        cli.main(["shutdown"])
    assert cli_runs == [["systemctl", "poweroff"]]


def test_cli_boot_powers_off_when_context_fails(monkeypatch, cli_runs):
    monkeypatch.setattr(cli, "_base_context", lambda paths: _fail())
    with pytest.raises(SystemExit):
        cli.main(["boot", "--skip-fetch"])
    assert cli_runs == [["systemctl", "poweroff"]]


def test_cli_boot_stays_on_when_context_fails_and_hold_exists(monkeypatch, cli_runs, tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state/hold").write_text("")
    monkeypatch.setattr(cli, "_base_context", lambda paths: _fail())
    with pytest.raises(SystemExit):
        cli.main(["boot"])
    assert cli_runs == []


def test_cli_boot_passes_skip_fetch(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_base_context", lambda paths: "CTX")
    monkeypatch.setattr(lifecycle, "boot", lambda ctx, fetch: seen.append(fetch))
    cli.main(["boot", "--skip-fetch"])
    cli.main(["boot"])
    assert seen == [False, True]


# --- bootstrap.sh -----------------------------------------------------------

BOOTSTRAP = Path(__file__).resolve().parents[1] / "bootstrap.sh"


def _run_bootstrap(tmp_path, hold=False):
    """Run bootstrap.sh with no host.env and fake shutdown/systemctl commands."""
    bin_dir, root = tmp_path / "bin", tmp_path / "root"
    bin_dir.mkdir()
    (root / "state").mkdir(parents=True)
    if hold:
        (root / "state/hold").write_text("")
    log = tmp_path / "calls.log"
    for name in ["shutdown", "systemctl"]:
        fake = bin_dir / name
        fake.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
        fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "ZOMBOID_ROOT": str(root)}
    proc = subprocess.run(["bash", str(BOOTSTRAP)], env=env, capture_output=True, text=True)
    return proc, log.read_text().splitlines()


def test_bootstrap_schedules_failsafe_and_powers_off_on_error(tmp_path):
    proc, calls = _run_bootstrap(tmp_path)
    assert proc.returncode != 0
    assert calls == ["shutdown -P +500", "systemctl poweroff"]


def test_bootstrap_stays_on_with_hold(tmp_path):
    proc, calls = _run_bootstrap(tmp_path, hold=True)
    assert proc.returncode != 0
    assert calls == ["shutdown -P +500"]


# --- user data --------------------------------------------------------------

USER_DATA = Path(__file__).resolve().parents[2] / "infra/main/user_data.sh.tftpl"


def test_user_data_enables_failsafe_unit_before_any_install():
    text = USER_DATA.read_text()
    lines = text.splitlines()
    first_apt = next(i for i, line in enumerate(lines) if line.startswith(("apt-get", "dpkg", "add-apt")))
    enable = lines.index("systemctl enable zomboid-failsafe.service")
    assert lines.index("shutdown -P +60") < enable < first_apt
    assert lines.index("cat > /etc/systemd/system/zomboid-failsafe.service <<'UNIT'") < enable
    assert "ExecStart=/sbin/shutdown -P +500" in lines
    assert "Type=oneshot" in lines
    assert "WantedBy=multi-user.target" in lines
    # The unit must run before bootstrap.sh, so it never replaces the shorter schedule.
    assert "Before=zomboid-boot.service" in lines


def test_bundle_units_do_not_replace_failsafe_unit():
    units = Path(__file__).resolve().parents[1] / "systemd"
    assert not (units / "zomboid-failsafe.service").exists()


def test_run_command_sets_home_when_systemd_gives_none(monkeypatch):
    # systemd services have no HOME, and the steamcmd wrapper stops without it.
    monkeypatch.delenv("HOME", raising=False)
    assert lifecycle.run_command(["sh", "-c", 'test "$HOME" = /root']) == 0


def test_run_command_keeps_an_existing_home(monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    assert lifecycle.run_command(["sh", "-c", 'test "$HOME" = /home/someone']) == 0
