import fcntl
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from zomboid_host import notify
from zomboid_host.bundle import fetch_bundle
from zomboid_host.backup import backup_key, list_backups, make_archive, restore, upload_and_prune
from zomboid_host.rcon import Rcon, RconError
from zomboid_host.settings import Settings, load_settings, render_ini
from zomboid_host.status import write_status
from zomboid_host.watchdog import WatchState, save_state

log = logging.getLogger(__name__)

RESTORE_PARAM = "/zomboid/restore_key"
GAME_PASSWORD_PARAM = "/zomboid/game_password"
RCON_PASSWORD_PARAM = "/zomboid/rcon_password"
WEBHOOK_PARAM = "/zomboid/discord_webhook_url"
STEAMCMD = "/usr/games/steamcmd"
APP_ID = "380870"
IMDS = "http://169.254.169.254/latest"

RCON_POLL_SECONDS = 5
RCON_WAIT_MINUTES = 15
SAVE_WAIT_SECONDS = 10
QUIT_WAIT_SECONDS = 300
FAILSAFE_EXTRA_MINUTES = 20
# Used before settings.toml is read: the default hard limit (480) + 20.
DEFAULT_FAILSAFE_MINUTES = 500

READY_MESSAGE = (
    "The server is ready.\nAddress: {address}\nPassword: {password}\n"
    "The address changes at each start. In the game, edit your saved server and change only the IP.\n"
    "The server stops after 30 minutes with no players, or after 8 hours."
)
BOOT_FAILED_MESSAGE = "The server did not start. The server will shut down."
RESTORE_FAILED_MESSAGE = (
    "The server did not start. The restore of back-up {key} failed. The server will shut down."
)
RESTORED_MESSAGE = "Restored back-up {key}."
BACKUP_FAILED_MESSAGE = "The back-up failed. The world is still on the server disk."
STOPPED_MESSAGE = "The server stopped ({reason_text}). The world is saved."
REASON_TEXT = {
    "idle": "no players for 30 minutes",
    "hard_limit": "8-hour limit",
    "admin": "stopped by an admin",
    "boot_failed": "start-up failed",
    "crash": "the game stopped unexpectedly",
}


class RestoreFailed(Exception):
    def __init__(self, key: str):
        super().__init__(f"cannot restore {key}")
        self.key = key


@dataclass(frozen=True)
class Paths:
    root: Path = Path("/opt/zomboid")

    @property
    def server(self) -> Path:
        return self.root / "server"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def bundle(self) -> Path:
        return self.root / "bundle"

    @property
    def marker(self) -> Path:
        return self.state / "clean_shutdown"

    @property
    def watch(self) -> Path:
        return self.state / "watch.json"

    @property
    def game_env(self) -> Path:
        return self.state / "game.env"

    @property
    def host_env(self) -> Path:
        return self.state / "host.env"

    @property
    def shutdown_lock(self) -> Path:
        return self.state / "shutdown.lock"

    @property
    def hold(self) -> Path:
        """If this file exists, a failed boot does not power off (for debugging)."""
        return self.state / "hold"


def run_command(cmd: list[str]) -> int:
    log.info("run: %s", " ".join(cmd))
    # systemd services have no HOME. The steamcmd wrapper stops without it.
    env = {**os.environ, "HOME": os.environ.get("HOME", "/root")}
    try:
        return subprocess.run(cmd, check=False, env=env).returncode
    except OSError:
        log.exception("cannot run %s", cmd[0])
        return 127


def imds_get(path: str) -> str:
    token_req = urllib.request.Request(
        f"{IMDS}/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"},
    )
    with urllib.request.urlopen(token_req, timeout=2) as resp:
        token = resp.read().decode()
    req = urllib.request.Request(
        f"{IMDS}/meta-data/{path}", headers={"X-aws-ec2-metadata-token": token}
    )
    with urllib.request.urlopen(req, timeout=2) as resp:
        return resp.read().decode().strip()


def imds_public_ip() -> str:
    return imds_get("public-ipv4")


def proc_boot_time() -> datetime:
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    return datetime.now(UTC) - timedelta(seconds=uptime)


def utc_now() -> datetime:
    return datetime.now(UTC)


def local_rcon(ctx: "HostContext") -> Rcon:
    return Rcon("127.0.0.1", ctx.settings.rcon_port, ctx.rcon_password)


@dataclass
class HostContext:
    settings: Settings | None
    s3: Any
    ssm: Any
    bucket: str
    webhook_url: str = ""
    rcon_password: str = ""
    game_password: str = ""
    run: Callable[[list[str]], int] = run_command
    paths: Paths = field(default_factory=Paths)
    now: Callable[[], datetime] = utc_now
    sleep: Callable[[float], None] = time.sleep
    public_ip: Callable[[], str] = imds_public_ip
    rcon_factory: Callable[["HostContext"], Rcon] = local_rcon
    boot_time: Callable[[], datetime] = proc_boot_time

    def open_rcon(self) -> Rcon:
        return self.rcon_factory(self)

    def address(self) -> str:
        return f"{self.public_ip()}:{self.settings.game_port}"


def _get_param(ssm, name: str) -> str:
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def load_secrets(ctx: HostContext) -> None:
    # The webhook comes first, so that a later failure can still be posted.
    ctx.webhook_url = _get_param(ctx.ssm, WEBHOOK_PARAM)
    ctx.game_password = _get_param(ctx.ssm, GAME_PASSWORD_PARAM)
    ctx.rcon_password = _get_param(ctx.ssm, RCON_PASSWORD_PARAM)
    for name, value in [
        (GAME_PASSWORD_PARAM, ctx.game_password),
        (RCON_PASSWORD_PARAM, ctx.rcon_password),
    ]:
        if not value.strip():
            raise ValueError(f"{name} is empty")


def power_off_unless_held(run: Callable[[list[str]], int], paths: Paths) -> None:
    if paths.hold.exists():
        log.warning("%s exists; the instance stays on", paths.hold)
        return
    run(["systemctl", "poweroff"])


def _env_value(value: str) -> str:
    """Quote a value for a systemd EnvironmentFile."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_game_env(ctx: HostContext) -> None:
    path = ctx.paths.game_env
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"SERVER_NAME={ctx.settings.server_name}\n")
        f.write(f"ADMIN_PASSWORD={_env_value(ctx.rcon_password)}\n")


def _write_status(ctx: HostContext, state: str, players, address, session_start) -> None:
    # A status write failure must never stop a boot or a shutdown.
    try:
        write_status(ctx.ssm, state, players, address, session_start)
    except Exception:
        log.exception("cannot write status %s", state)


def _world_exists(ctx: HostContext) -> bool:
    return (ctx.paths.data / "Saves/Multiplayer" / ctx.settings.server_name).is_dir()


def backup(ctx: HostContext, reason: str) -> str:
    key = backup_key(ctx.now(), reason)
    ctx.paths.state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ctx.paths.state) as tmp:
        archive = make_archive(ctx.paths.data, ctx.settings.server_name, Path(tmp) / "world.tar.zst")
        upload_and_prune(ctx.s3, ctx.bucket, archive, key, ctx.settings.backup_retain)
    log.info("backed up the world to %s", key)
    return key


# --- boot -------------------------------------------------------------------


def _install_config(ctx: HostContext) -> None:
    """Copy the repo config into data/Server/. The repo config always wins."""
    s = ctx.settings
    src = ctx.paths.bundle / "config/server"
    dest = ctx.paths.data / "Server"
    dest.mkdir(parents=True, exist_ok=True)
    ini_name = f"{s.server_name}.ini"
    if src.is_dir():
        for f in sorted(src.iterdir()):
            if f.is_file() and f.name != ini_name:
                shutil.copyfile(f, dest / f.name)
    template_path = src / ini_name if (src / ini_name).is_file() else dest / ini_name
    template = template_path.read_text() if template_path.is_file() else ""
    overrides = {
        "Password": ctx.game_password,
        "RCONPassword": ctx.rcon_password,
        "RCONPort": str(s.rcon_port),
        "SaveWorldEveryMinutes": str(s.autosave_minutes),
        "DefaultPort": str(s.game_port),
    }
    (dest / ini_name).write_text(render_ini(template, overrides))


def _set_java_xmx(ctx: HostContext) -> None:
    path = ctx.paths.server / "ProjectZomboid64.json"
    if not path.is_file():
        log.warning("%s not found; -Xmx not set", path)
        return
    data = json.loads(path.read_text())
    xmx = f"-Xmx{ctx.settings.java_xmx}"
    args = data.get("vmArgs", [])
    if any(a.startswith("-Xmx") for a in args):
        args = [xmx if a.startswith("-Xmx") else a for a in args]
    else:
        args = [*args, xmx]
    data["vmArgs"] = args
    path.write_text(json.dumps(data, indent=2) + "\n")


def _wait_for_rcon(ctx: HostContext) -> bool:
    deadline = ctx.now() + timedelta(minutes=RCON_WAIT_MINUTES)
    while True:
        try:
            with ctx.open_rcon() as rcon:
                rcon.command("players")
            return True
        except RconError as exc:
            log.info("RCON not ready: %s", exc)
        if ctx.now() >= deadline:
            return False
        ctx.sleep(RCON_POLL_SECONDS)


def _restore(ctx: HostContext, key: str) -> None:
    try:
        ctx.paths.data.mkdir(parents=True, exist_ok=True)
        restore(ctx.s3, ctx.bucket, key, ctx.paths.data, ctx.settings.server_name)
    except Exception as exc:
        raise RestoreFailed(key) from exc


def _start(ctx: HostContext) -> bool:
    """Boot steps 3-13. Returns False if the game does not answer RCON in time."""
    s, p = ctx.settings, ctx.paths
    restore_key = ctx.ssm.get_parameter(Name=RESTORE_PARAM)["Parameter"]["Value"]
    if restore_key != "none":
        _restore(ctx, restore_key)
        ctx.ssm.put_parameter(Name=RESTORE_PARAM, Value="none", Type="String", Overwrite=True)
        notify.post(ctx.webhook_url, RESTORED_MESSAGE.format(key=restore_key))
    elif not _world_exists(ctx):
        # A new disk (for example a replaced instance) loads the newest back-up.
        backups = list_backups(ctx.s3, ctx.bucket)
        if backups:
            _restore(ctx, backups[0])
            notify.post(ctx.webhook_url, RESTORED_MESSAGE.format(key=backups[0]))
    elif not p.marker.exists():
        try:
            backup(ctx, "pre-update")
        except Exception:
            log.exception("pre-update back-up failed; the update continues")
    p.marker.unlink(missing_ok=True)

    code = ctx.run([
        STEAMCMD,
        "+force_install_dir", str(p.server),
        "+login", "anonymous",
        "+app_update", APP_ID, "validate",
        "+quit",
    ])
    if code != 0:
        log.warning("steamcmd exited with code %s", code)
    # A long update must not use up the failsafe margin.
    ctx.run(["shutdown", "-P", f"+{s.hard_limit_minutes + FAILSAFE_EXTRA_MINUTES}"])

    _install_config(ctx)
    _set_java_xmx(ctx)
    _write_game_env(ctx)
    # Boot runs as root; the game runs as zomboid and must own its files.
    ctx.run(["chown", "-R", "zomboid:zomboid", str(p.server), str(p.data)])

    now = ctx.now()
    save_state(p.watch, WatchState(now, now, set()))
    address = ctx.address()
    _write_status(ctx, "starting", None, address, now)
    ctx.run(["systemctl", "start", "zomboid-game"])

    if not _wait_for_rcon(ctx):
        return False
    _write_status(ctx, "running", 0, address, now)
    notify.post(ctx.webhook_url, READY_MESSAGE.format(address=address, password=ctx.game_password))
    ctx.run(["systemctl", "start", "zomboid-watchdog.timer"])
    return True


def _boot_failed(ctx: HostContext, game_may_run: bool, message: str = BOOT_FAILED_MESSAGE) -> None:
    if ctx.webhook_url:
        notify.post(ctx.webhook_url, message)
    if ctx.paths.hold.exists():
        log.warning("%s exists; the instance stays on", ctx.paths.hold)
    elif game_may_run:
        shutdown(ctx, "boot_failed")
    else:
        ctx.run(["systemctl", "poweroff"])


def boot(ctx: HostContext, fetch: bool = True) -> None:
    # OS-level failsafe first, before any file or network read: power off
    # even if this program or the watchdog fails.
    ctx.run(["shutdown", "-P", f"+{DEFAULT_FAILSAFE_MINUTES}"])
    try:
        if fetch:
            fetch_bundle(ctx.s3, ctx.bucket, ctx.paths.bundle)
        ctx.settings = load_settings(ctx.paths.bundle / "config/settings.toml")
        failsafe = ctx.settings.hard_limit_minutes + FAILSAFE_EXTRA_MINUTES
        if failsafe != DEFAULT_FAILSAFE_MINUTES:
            ctx.run(["shutdown", "-P", f"+{failsafe}"])
        load_secrets(ctx)
    except Exception:
        log.exception("boot failed before the game start")
        _boot_failed(ctx, game_may_run=False)
        return
    message = BOOT_FAILED_MESSAGE
    try:
        started = _start(ctx)
    except RestoreFailed as exc:
        log.exception("boot failed")
        started = False
        message = RESTORE_FAILED_MESSAGE.format(key=exc.key)
    except Exception:
        log.exception("boot failed")
        started = False
    if not started:
        _boot_failed(ctx, game_may_run=True, message=message)


# --- shutdown ---------------------------------------------------------------


def _save_and_quit(ctx: HostContext) -> bool:
    """Returns True if `quit` was sent. RCON down means the save and quit are skipped."""
    try:
        with ctx.open_rcon() as rcon:
            rcon.command("save")
            ctx.sleep(SAVE_WAIT_SECONDS)
            try:
                rcon.command("quit")
            except RconError:
                # The game can close the connection before it replies.
                log.info("no reply to quit")
            return True
    except RconError:
        log.warning("RCON is down; save and quit skipped", exc_info=True)
        return False


def _wait_for_game_exit(ctx: HostContext) -> bool:
    deadline = ctx.now() + timedelta(seconds=QUIT_WAIT_SECONDS)
    while ctx.run(["systemctl", "is-active", "--quiet", "zomboid-game"]) == 0:
        if ctx.now() >= deadline:
            return False
        ctx.sleep(RCON_POLL_SECONDS)
    return True


def _try_lock(path: Path) -> int | None:
    """Take an exclusive lock that lasts until this process exits. None if another holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def shutdown(ctx: HostContext, reason: str) -> None:
    # The watchdog and /stop can both start a shutdown. Only the first one runs.
    if _try_lock(ctx.paths.shutdown_lock) is None:
        log.info("shutdown (%s) skipped: another shutdown runs", reason)
        return
    log.info("shutdown: %s", reason)
    try:
        _write_status(ctx, "stopping", None, None, None)
        ctx.run(["systemctl", "stop", "zomboid-watchdog.timer"])
        quit_sent = _save_and_quit(ctx)
        exited = _wait_for_game_exit(ctx)
        if not exited:
            ctx.run(["systemctl", "kill", "-s", "KILL", "zomboid-game"])

        if _world_exists(ctx):
            try:
                backup(ctx, "shutdown")
            except Exception:
                log.exception("shutdown back-up failed")
                notify.post(ctx.webhook_url, BACKUP_FAILED_MESSAGE)

        if quit_sent and exited:
            ctx.paths.marker.parent.mkdir(parents=True, exist_ok=True)
            ctx.paths.marker.write_text("")

        _write_status(ctx, "stopped", None, None, None)
        reason_text = REASON_TEXT.get(reason, reason)
        notify.post(ctx.webhook_url, STOPPED_MESSAGE.format(reason_text=reason_text))
    finally:
        ctx.run(["systemctl", "poweroff"])
