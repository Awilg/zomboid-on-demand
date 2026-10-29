import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    server_name: str
    idle_minutes: int = 30
    hard_limit_minutes: int = 480
    warn_minutes: tuple[int, ...] = (15, 5)
    discord_warn_minutes: int = 15
    autosave_minutes: int = 10
    backup_retain: int = 14
    java_xmx: str = "6g"
    rcon_port: int = 27015
    game_port: int = 16261


def load_settings(path: Path) -> Settings:
    data = tomllib.loads(Path(path).read_text())
    known = {f.name for f in fields(Settings)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")
    if "warn_minutes" in data:
        data["warn_minutes"] = tuple(data["warn_minutes"])
    return Settings(**data)


def render_ini(template: str, overrides: dict[str, str]) -> str:
    remaining = dict(overrides)
    lines = []
    for line in template.splitlines():
        key = line.split("=", 1)[0] if "=" in line else None
        if key in remaining:
            line = f"{key}={remaining.pop(key)}"
        lines.append(line)
    lines.extend(f"{k}={v}" for k, v in remaining.items())
    return "\n".join(lines) + "\n" if lines else ""
