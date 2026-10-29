import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from zomboid_host import notify
from zomboid_host.rcon import RconError, parse_player_count
from zomboid_host.settings import Settings
from zomboid_host.status import write_status

if TYPE_CHECKING:
    from zomboid_host.lifecycle import HostContext

log = logging.getLogger(__name__)

GAME_WARNING = 'servermsg "Server restart in {n} minutes. The world is saved."'
DISCORD_WARNING = (
    "The server stops in 15 minutes (8-hour limit). Type /start after the stop to continue."
)


@dataclass
class WatchState:
    session_start: datetime
    last_player_seen: datetime
    warned: set[int]


@dataclass(frozen=True)
class Action:
    kind: Literal["warn_game", "warn_discord", "shutdown"]
    minutes_left: int | None = None
    reason: str | None = None


def load_state(path: Path) -> WatchState | None:
    path = Path(path)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return WatchState(
        session_start=datetime.fromisoformat(data["session_start"]),
        last_player_seen=datetime.fromisoformat(data["last_player_seen"]),
        warned=set(data["warned"]),
    )


def save_state(path: Path, state: WatchState) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {
            "session_start": state.session_start.isoformat(),
            "last_player_seen": state.last_player_seen.isoformat(),
            "warned": sorted(state.warned),
        }
    )
    # Write a temp file and rename it, so a crash never leaves a half-written file.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def decide(now: datetime, players: int | None, state: WatchState, s: Settings) -> list[Action]:
    if players:
        state.last_player_seen = now

    elapsed = now - state.session_start
    if elapsed >= timedelta(minutes=s.hard_limit_minutes):
        return [Action("shutdown", reason="hard_limit")]
    if now - state.last_player_seen >= timedelta(minutes=s.idle_minutes):
        return [Action("shutdown", reason="idle")]

    minutes_left = (timedelta(minutes=s.hard_limit_minutes) - elapsed).total_seconds() / 60
    actions: list[Action] = []
    for w in sorted(s.warn_minutes, reverse=True):
        if minutes_left <= w and w not in state.warned:
            state.warned.add(w)
            actions.append(Action("warn_game", minutes_left=w))
            if w == s.discord_warn_minutes:
                actions.append(Action("warn_discord", minutes_left=w))
    return actions


def _load_or_boot_state(ctx: "HostContext") -> WatchState:
    try:
        state = load_state(ctx.paths.watch)
    except (ValueError, KeyError, TypeError):
        log.exception("watch state is corrupt; the clock starts at system boot")
        state = None
    if state is None:
        # Never "now": a lost file must not extend the hard limit.
        booted = ctx.boot_time()
        state = WatchState(booted, booted, set())
    return state


def _count_players(ctx: "HostContext") -> int | None:
    try:
        with ctx.open_rcon() as rcon:
            return parse_player_count(rcon.command("players"))
    except RconError as exc:
        log.warning("cannot count players: %s", exc)
        return None


def run_once(ctx: "HostContext", now: datetime | None = None) -> None:
    from zomboid_host import lifecycle

    now = now or ctx.now()
    # The game service has Restart=no, so an inactive game never comes back.
    if ctx.run(["systemctl", "is-active", "--quiet", "zomboid-game"]) != 0:
        log.warning("zomboid-game is not active")
        lifecycle.shutdown(ctx, "crash")
        return
    state = _load_or_boot_state(ctx)
    players = _count_players(ctx)
    actions = decide(now, players, state, ctx.settings)
    save_state(ctx.paths.watch, state)
    try:
        address = ctx.address()
    except Exception:
        log.exception("cannot read the public IP")
        address = None
    try:
        write_status(ctx.ssm, "running", players, address, state.session_start)
    except Exception:
        log.exception("cannot write status")

    for action in actions:
        if action.kind == "warn_game":
            try:
                with ctx.open_rcon() as rcon:
                    rcon.command(GAME_WARNING.format(n=action.minutes_left))
            except RconError as exc:
                log.warning("cannot send the in-game warning: %s", exc)
        elif action.kind == "warn_discord":
            notify.post(ctx.webhook_url, DISCORD_WARNING)
        elif action.kind == "shutdown":
            lifecycle.shutdown(ctx, action.reason)
            return
