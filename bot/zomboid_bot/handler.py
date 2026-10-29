import base64
import functools
import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from zomboid_bot import verify

log = logging.getLogger(__name__)

STATUS_PARAMETER = "/zomboid/status"
STOP_COMMAND = "/opt/zomboid/venv/bin/zomboid-host shutdown --reason admin || systemctl poweroff"

NEED_ROLE = "You need the zomboid role to use this command."
NEED_ADMIN = "Only admins can stop the server."
STARTING = "Starting the server. The address comes here in about 3 minutes."
ALREADY_STARTING = "The server is already starting. Wait for the address."
SHUTTING_DOWN = "The server is shutting down. Try again in one minute."
STOPPING = "Stopping the server. The world is saved first."
NOT_RUNNING = "The server is not running."
UNKNOWN_COMMAND = "Unknown command."
FAILED = "Something went wrong. Try again in one minute."

EPHEMERAL = 64


@functools.cache
def client(name: str):
    return boto3.client(name, region_name=os.environ.get("AWS_REGION"))


def now() -> datetime:
    return datetime.now(timezone.utc)


def lambda_handler(event, context):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not verify.is_valid(
        os.environ["DISCORD_PUBLIC_KEY"],
        headers.get("x-signature-ed25519", ""),
        headers.get("x-signature-timestamp", ""),
        body,
    ):
        return _response({"error": "invalid request signature"}, status=401)

    interaction = json.loads(body)
    if interaction.get("type") == 1:
        return _response({"type": 1})
    try:
        content, flags = _run_command(interaction)
    except Exception:
        log.exception("command failed")
        content, flags = FAILED, EPHEMERAL
    return _response({"type": 4, "data": {"content": content, "flags": flags}})


def _response(payload: dict, status: int = 200) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _run_command(interaction: dict) -> tuple[str, int]:
    name = (interaction.get("data") or {}).get("name")
    roles = (interaction.get("member") or {}).get("roles") or []
    if name in ("start", "status"):
        if os.environ["ZOMBOID_ROLE_ID"] not in roles:
            return NEED_ROLE, EPHEMERAL
        return (_start() if name == "start" else _status()), 0
    if name == "stop":
        if os.environ["ADMIN_ROLE_ID"] not in roles:
            return NEED_ADMIN, EPHEMERAL
        return _stop(), 0
    return UNKNOWN_COMMAND, EPHEMERAL


def _describe() -> tuple[str, datetime]:
    reservations = client("ec2").describe_instances(InstanceIds=[os.environ["INSTANCE_ID"]])
    instance = reservations["Reservations"][0]["Instances"][0]
    return instance["State"]["Name"], instance["LaunchTime"]


def _read_status() -> dict:
    try:
        value = client("ssm").get_parameter(Name=STATUS_PARAMETER)["Parameter"]["Value"]
    except ClientError as error:
        if error.response["Error"]["Code"] == "ParameterNotFound":
            return {"state": "stopped"}
        raise
    return json.loads(value)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _effective_state(ec2_state: str, launch_time: datetime, status: dict) -> str:
    """Combine the EC2 state with the state the instance reports about itself."""
    if ec2_state == "pending":
        return "starting"
    if ec2_state in ("stopping", "shutting-down", "terminated"):
        return "stopping"
    if ec2_state != "running":
        return "stopped"
    # A status written before this launch is left from the last session,
    # so the instance is still in its boot.
    updated_at = _parse_time(status.get("updated_at"))
    if updated_at is None or updated_at < launch_time:
        return "starting"
    reported = status.get("state")
    if reported in ("starting", "running", "stopping"):
        return reported
    # The host writes "stopped" just before it powers off.
    if reported == "stopped":
        return "stopping"
    return "starting"


def _state_and_status() -> tuple[str, dict]:
    ec2_state, launch_time = _describe()
    status = _read_status() if ec2_state == "running" else {}
    return _effective_state(ec2_state, launch_time, status), status


def _start() -> str:
    state, status = _state_and_status()
    if state == "stopped":
        try:
            client("ec2").start_instances(InstanceIds=[os.environ["INSTANCE_ID"]])
        except ClientError as error:
            if error.response["Error"]["Code"] == "IncorrectInstanceState":
                return SHUTTING_DOWN
            raise
        return STARTING
    if state == "starting":
        return ALREADY_STARTING
    if state == "running":
        return _status_text(state, status)
    return SHUTTING_DOWN


def _status() -> str:
    state, status = _state_and_status()
    return _status_text(state, status)


def _status_text(state: str, status: dict) -> str:
    if state == "stopped":
        return f"State: {state}. Players: 0."
    # A "starting" state can come from a status left from the last session.
    players = None if state == "starting" else status.get("players")
    text = f"State: {state}. Players: {'unknown' if players is None else players}."
    if state in ("starting", "stopping"):
        return text
    text += f" Address: {status.get('address') or 'unknown'}."
    session_start = status.get("session_start")
    if session_start:
        text += f" Time left: {_time_left(session_start)}."
    return text


def _time_left(session_start: str) -> str:
    elapsed = max(0, (now() - _parse_time(session_start)).total_seconds() // 60)
    left = max(0, int(os.environ["HARD_LIMIT_MINUTES"]) - int(elapsed))
    return f"{left // 60}h {left % 60}m"


def _stop() -> str:
    if _describe()[0] != "running":
        return NOT_RUNNING
    client("ssm").send_command(
        InstanceIds=[os.environ["INSTANCE_ID"]],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [STOP_COMMAND]},
    )
    return STOPPING
