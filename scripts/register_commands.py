#!/usr/bin/env python3
"""Register the /start, /status and /stop guild commands with Discord.

Usage: DISCORD_BOT_TOKEN=... register_commands.py --app-id ID --guild-id ID
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

COMMANDS = [
    ("start", "Start the Zomboid server."),
    ("status", "Show the server state."),
    ("stop", "Stop the server (admins only)."),
]


def build_payload() -> list[dict]:
    return [{"name": name, "description": desc, "type": 1} for name, desc in COMMANDS]


def build_request(app_id: str, guild_id: str, token: str) -> urllib.request.Request:
    url = f"https://discord.com/api/v10/applications/{app_id}/guilds/{guild_id}/commands"
    return urllib.request.Request(
        url,
        data=json.dumps(build_payload()).encode(),
        method="PUT",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "zomboid-server-register (local script, 1.0)",
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--guild-id", required=True)
    args = parser.parse_args()
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        sys.exit("error: set DISCORD_BOT_TOKEN")
    try:
        with urllib.request.urlopen(build_request(args.app_id, args.guild_id, token)) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        print(f"HTTP {err.code}: {err.read().decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)
    print(f"HTTP {status}")
    print("Registered:", ", ".join(c["name"] for c in json.loads(body)))


if __name__ == "__main__":
    main()
