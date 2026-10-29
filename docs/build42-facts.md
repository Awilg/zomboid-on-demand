# Build 42 server facts

Checked on 2026-10-27. Live check on 2026-10-29: server version 42.21.0. Status: C = confirmed from an official source or the PZwiki. U = UNCONFIRMED.
The PZwiki blocks direct fetches (Cloudflare). The wiki text comes from Wayback copies dated 2026-07-31 (see URLs).

## Branch status (read this first)

Build 42 multiplayer is on the stable branch. It is NOT behind a beta flag.
- C. 42.20.0 Stable released 2026-07-29. 42.21 Stable released 2026-09-28. Current stable is 42.21.
- C. 2026-10-27: the Unstable branch is renamed "Pre-Release". It now holds 42.22. Do not use it for this server.
- Source: https://store.steampowered.com/news/app/108600/view/717912016810934441 (42.20.0)
- Source: https://store.steampowered.com/news/app/108600/view/688643694674315246 (42.21)
- Source: https://store.steampowered.com/news/app/108600/view/672883630592236406 (Pre-Release rename)
- Source: https://store.steampowered.com/feeds/news/app/108600/ (full list)
- The Shockbyte claim is correct.

## Facts

| Item | Value | Status | Source |
|---|---|---|---|
| (a) SteamCMD branch | `public` (no `-beta` flag). Command: `+app_update 380870 validate`. Old build: `-beta legacy41`. | C | https://pzwiki.net/wiki/Dedicated_server and the Steam news posts above |
| (b) Ports | UDP `16261` (DefaultPort) and UDP `16262` (UDPPort) | C | https://pzwiki.net/wiki/Dedicated_server ; https://pzwiki.net/wiki/Server_settings |
| (c) Save path | `<cachedir>/Saves/Multiplayer/<servername>/` | C | https://pzwiki.net/wiki/Dedicated_server |
| (c) Config path | `<cachedir>/Server/<servername>.ini`, `_SandboxVars.lua`, `_spawnregions.lua` | C | https://pzwiki.net/wiki/Dedicated_server |
| (c) `db` path | `<cachedir>/db/<servername>.db` (player and whitelist database) | C | Live check 2026-10-29: `db/Sullivan.db` exists after the first start. |
| (d) Mod keys | `Mods=` (mod IDs) and `WorkshopItems=` (Workshop numeric IDs). Both use `;` separators. | C | https://pzwiki.net/wiki/Server_settings |
| (d) Build 42 `\` prefix | Some guides say each ID in `Mods=` needs a `\` prefix, for example `Mods=\ModA;\ModB`. One guide says no change. | U | https://winternode.com/help/games/project-zomboid/setup/adding-mods-to-your-project-zomboid-server ; https://supercraft.host/wiki/project-zomboid/how_to_add_mods_project_zomboid_server/ |
| (e) Key names | `SaveWorldEveryMinutes` (default 0), `RCONPort` (default 27015), `RCONPassword`, `Password`. All exist. | C | https://pzwiki.net/wiki/Server_settings |
| (f) Java memory file | `ProjectZomboid64.json` in the server install dir. Edit `-Xmx` in `vmArgs`. Linux default is about 8g. | C | Live check 2026-10-29: the file exists, and the host set `-Xmx6g` in it. https://pzwiki.net/wiki/Startup_parameters (names the file); https://steamcommunity.com/app/108600/discussions/0/5845107457330406260 |
| (g) Linux start script | `start-server.sh` (run `bash start-server.sh -servername NAME`) | C | https://pzwiki.net/wiki/Dedicated_server |
| (h) Cache dir flag | `-cachedir=<path>` is still valid. `-Ddeployment.user.cachedir=<path>` also works on Linux. | C | https://pzwiki.net/wiki/Startup_parameters |
| (i) RCON `players` reply | The console command `players` lists connected players. The wiki does not document the RCON reply text. Best value: first line `Players connected (N):`, then one line per player starting with `-`. Parse the count with a regex and test on a real server. | C | Live check 2026-10-29: the reply is `Players connected (0): \n`. `parse_player_count` reads it as 0. Not yet checked with a player connected. |

## Other notes

- The wiki says the start script sets a high default memory. Set `-Xmx` in `ProjectZomboid64.json` to match `java_xmx` in the plan.
- On a first run the server asks for an admin password. Pass `-adminpassword` to avoid the prompt.
- `SaveWorldEveryMinutes` defaults to 0, so the plan must set it to 10.
- `-nosteam` is not needed on a Steam server.
- Many Workshop mods are not yet republished for stable. Check each mod.
- The steamcmd licence prompt does not stop the first boot. Live check on 2026-10-29.
- steamcmd needs `HOME`. systemd services do not set it, so `run_command` sets `HOME=/root`.
