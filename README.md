# Zomboid on demand

This project runs a Project Zomboid (Build 42) dedicated server on demand. The server runs on an AWS EC2 instance. Players start it with a Discord slash command, so you pay for compute only while somebody plays.

The server stops by itself when no player is on for 30 minutes. It also stops after 8 hours. Each stop saves the world to an S3 back-up.

## Commands in Discord

| Command | Who can use it | What it does |
|---|---|---|
| `/start` | Members with the `zomboid` role | Starts the server. The bot posts the address and the password when the game is ready. |
| `/status` | Members with the `zomboid` role | Shows the server state. |
| `/stop` | Admins | Stops the server and saves the world. |

## Join the server

Send this section to the players.

The server keeps your character. The server finds your character from your username. You do not select it from a list.

1. Type `/start` in Discord. Wait for the "The server is ready" message.
2. Start Project Zomboid. Always use the same Steam account. The server links each account to a Steam ID.
3. Select **Join**, then **Favorites**.
4. The first time, click **Add Server**. Enter the address and the password from the message.
5. At later starts, click **Edit** on your saved server. Change only the IP. The address changes at each start. Do not add a new server, or you lose your saved logins.
6. Log in with your username and your account password. The account password is not the server password.

If your character dies, you make a new character.

If the world comes from an old server (see "Import an existing world"), your old character is in it:

- Log in with the same username as on the old server. To find it, look at your saved accounts for the old server.
- Enter your old account password, if you had one.
- If your character died on the old server, you make a new character.

The list of accounts below each saved server is on your computer. It is not a list of the characters on the server.

## Prerequisites

Install these tools on your computer:

- AWS CLI, with an AWS account you can use
- Terraform 1.9 or later
- Python 3.12
- `zstd`
- The AWS Session Manager plugin. The command `aws ssm start-session` needs it.

Make the Python environment:

```
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

## One-time set-up

The default AWS region is `us-east-2` (Ohio). To use a different region, set `region` in both Terraform stacks. Accounts from the new AWS sign-up ("projects") can use only `us-east-2`. To use another region, these accounts need the AWS "advanced features", which you cannot turn off.

1. Install AWS CLI 2.32 or later. Run `aws login --profile zomboid`, and select the project in the browser. In each terminal where you run `make`, run `export AWS_PROFILE=zomboid` first. The login lasts 12 hours. To renew it, run `aws login --profile zomboid` again.
2. Open the Discord developer portal. Make a new application.
3. Copy the public key. Copy the application ID.
4. Make a bot for the application. Copy the bot token.
5. Invite the bot to your Discord server. Use the `applications.commands` scope.
6. Pick the channel for server notices. Make a webhook in this channel. Copy the webhook URL.
7. Copy the ID of the `zomboid` role. Copy the ID of the admin role. Also copy the ID of your Discord server. To copy IDs, turn on Developer Mode in Discord (User Settings, then Advanced).
8. Set `server_name` in `config/settings.toml` to the name of your world. To import an existing world, use the name of its `.ini` file. See "Import an existing world".
9. Copy `infra/main/terraform.tfvars.example` to `infra/main/terraform.tfvars`. Fill in all values.
   - `game_password` must not be empty. It must not contain spaces or quotes.
   - `owner_email` receives the budget alerts.
10. Run `make deploy`. Type `yes` at each Terraform prompt.
11. Find the line below "Discord Interactions Endpoint URL:" in the output. This line is the `interactions_url`. Paste it in the Discord application, in the field "Interactions Endpoint URL". Save the change.
12. Register the slash commands:

```
DISCORD_BOT_TOKEN=<bot token> python3 scripts/register_commands.py --app-id <application ID> --guild-id <server ID>
```

If Discord refuses the URL with a 403 error, check both Lambda permissions in the AWS console.

## Import an existing world

You can move a world from another server to this server. For example, a world that a player hosted from the game on a PC.

1. The person who has the world follows `docs/world-export.md`. This makes a zip file.
2. Make sure `server_name` in `config/settings.toml` is the same as the name of the world's `.ini` file, without `.ini`.
3. Run `make deploy` once, if you did not do so yet. The import needs the S3 bucket.
4. Run `make import-world ZIP=world.zip`. Use the real path of the zip file.
5. The script copies the world's settings to `config/server/`. It removes the old passwords from the copy. It uploads the world as the newest back-up.
6. Run `make deploy`.
7. If the server runs now, type `/stop` in Discord. Wait for the "The server stopped" message.
8. Type `/start` in Discord. The server loads the imported world.

## Add a mod

1. Open `config/server/<name>.ini`.
2. Add the mod ID to `Mods=`. Add the Workshop number to `WorkshopItems=`. Use `;` between items.
3. Run `make deploy`.
4. Start the server. The new settings apply at the next boot.

Many Workshop mods do not work with Build 42 yet. Check each mod before you add it.

## Change the instance size

1. Stop the server. Type `/stop` in Discord.
2. Edit `instance_type` in `infra/main/terraform.tfvars`. The default is `m7i-flex.large`. The AWS Free plan allows only some instance types. To list them, run `aws ec2 describe-instance-types --filters Name=free-tier-eligible,Values=true`.
3. Run `make deploy`.

If you change the instance size, check `java_xmx` in `config/settings.toml`. The Java memory limit must fit in the memory of the instance.

## Restore a back-up

1. Run `make restore`.
2. Type the number of the back-up you want. The list shows the newest back-ups first.
3. If the server runs now, type `/stop` in Discord.
4. Type `/start`. The server loads the chosen back-up. The bot posts "Restored back-up" with the back-up name.

## Delete everything

1. If the server runs now, type `/stop` in Discord. Wait for the "The server stopped" message. The destroy makes no back-up, so the stop saves the last play time.
2. Run `make destroy`.

The destroy removes the server and the Lambda function. The back-ups stay in S3. No command in this project deletes back-ups.

## Costs

- The server costs about $0.10 per hour while it runs (m7i-flex.large, `us-east-2`, on-demand).
- The 40 GB gp3 disk costs about $3.20 per month, also while the server is stopped.
- The S3 back-ups cost a few cents per month.
- Example: 10 hours of play each week costs about $4 for compute. The total is about $7 to $8 per month.
- To save about $0.80 per month, set `disk_gb` to 30 in `terraform.tfvars`.
- The AWS Free plan ends after 6 months or when the credits run out. Upgrade to the Paid plan before then. If you do not upgrade within 90 days after the end, AWS deletes the account and the back-ups.
- Check the AWS price list for current prices.
- A budget alert sends an email to `owner_email` at $15 per month.

## Troubleshooting

Connect to the server:

```
aws ssm start-session --target <instance_id>
```

Get the `instance_id` with `terraform -chdir=infra/main output -raw instance_id`.

On the server, read the logs:

```
journalctl -u zomboid-boot -u zomboid-game
journalctl -u zomboid-watchdog
```

The first-boot log is `/var/log/cloud-init-output.log`.

### A failed boot powers off the server

When a boot fails, the server powers off. This can remove the evidence. To keep the server on:

1. Connect with `aws ssm start-session`.
2. Run `sudo touch /opt/zomboid/state/hold`.
3. Start the server again.

A failed boot now stays on. The failsafe still applies: the server powers off 8 hours 20 minutes after the boot. To cancel it, run `sudo shutdown -c`. Remove the `hold` file when you finish. Then stop the server, or it will keep running and cost money.

### The first boot fails

The first boot installs the host program from user data. User data runs only once. If the first boot fails, a restart does not repeat it. A failsafe still powers off the server 500 minutes after each boot.

1. Read `/var/log/zomboid-user-data.log` and `/var/log/cloud-init-output.log`. Find the cause and fix it.
2. Run `make deploy`.
3. Make a new instance. This also runs user data again:

```
terraform -chdir=infra/main apply -replace=aws_instance.zomboid -var backup_bucket=$(terraform -chdir=infra/data output -raw backup_bucket)
```

The new instance has a new, empty disk. The world comes from the newest back-up in S3.

## First live test

Do these steps once, after the first `make deploy`. Write the results down.

1. Run `make deploy`. Expected: Discord accepts the Interactions Endpoint URL.
2. A member without the `zomboid` role types `/start`. Expected: the bot replies with the role message. This first `/start` reply arrives within 3 seconds. Discord does not show "The application did not respond".
3. A `zomboid` member types `/start`. Expected: the "The server is ready" message with the address and the password. The first boot downloads the game, so it can take more than 5 minutes.
4. Join from a game client with the password. Expected: the imported world and the old characters.
5. Leave the server. Expected: after about 30 minutes, the "The server stopped" message. `aws ec2 describe-instances` shows `stopped`. A new `-shutdown` key is in S3.
6. Type `/start`, then `/stop` as an admin. Expected: the stopped message with "stopped by an admin".
7. Test the time limit:
   1. Set `hard_limit_minutes = 20` in `config/settings.toml`. Run `make deploy`.
   2. Type `/start`. Expected: in-game warnings, a Discord warning at 15 minutes left, and a stop at 20 minutes.
   3. Set the value back to `480`. Run `make deploy`.
8. Run `make restore`. Pick the second-newest back-up. Type `/start`. Expected: the "Restored back-up" message.

During the first real run, also check these items:

- RCON `players` reply text. Read `journalctl -u zomboid-watchdog`. The watchdog must parse the player count.
- The path `db/<server>.db`. Check that the file exists in the game data folder.
- Build 42 `Mods=` syntax. Some guides say each ID needs a `\` prefix. Keep the imported lines as they are. If mods fail to load, test the `\` prefix.
- The steamcmd licence prompt. Read `/var/log/cloud-init-output.log` for a prompt that stops the first boot.
- The Interactions Endpoint URL. If Discord reports 403, check both Lambda permissions.

See `docs/build42-facts.md` for the Build 42 facts and which ones are not confirmed.
