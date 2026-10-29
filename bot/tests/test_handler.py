import base64
import json
import time
from datetime import datetime, timedelta, timezone

import boto3
import botocore.waiter
import pytest
from moto import mock_aws
from nacl.signing import SigningKey

from zomboid_bot import handler

ZROLE = "111"
AROLE = "222"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
STOP_COMMAND = "/opt/zomboid/venv/bin/zomboid-host shutdown --reason admin || systemctl poweroff"
SHUTTING_DOWN = "The server is shutting down. Try again in one minute."
STATUS_RUNNING = {
    "state": "running",
    "players": 3,
    "address": "1.2.3.4:16261",
    "session_start": (NOW - timedelta(hours=2, minutes=30)).isoformat(),
    "updated_at": NOW.isoformat(),
}

LAUNCH = NOW - timedelta(hours=3)

KEY = SigningKey.generate()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", KEY.verify_key.encode().hex())
    monkeypatch.setenv("ZOMBOID_ROLE_ID", ZROLE)
    monkeypatch.setenv("ADMIN_ROLE_ID", AROLE)
    monkeypatch.setenv("HARD_LIMIT_MINUTES", "480")
    monkeypatch.setattr(handler, "now", lambda: NOW)
    handler.client.cache_clear()


@pytest.fixture
def aws(monkeypatch):
    with mock_aws():
        ec2 = boto3.client("ec2")
        image = ec2.describe_images()["Images"][0]["ImageId"]
        instance_id = ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
        monkeypatch.setenv("INSTANCE_ID", instance_id)
        yield ec2, instance_id


def put_status(status):
    # Moto sets LaunchTime from the real clock, so the status must be newer than that.
    status = dict(status, updated_at=datetime.now(timezone.utc).isoformat())
    boto3.client("ssm").put_parameter(Name="/zomboid/status", Value=json.dumps(status), Type="String")


def instance_state(ec2, instance_id):
    return ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]["State"]["Name"]


def stop_instance(ec2, instance_id):
    ec2.stop_instances(InstanceIds=[instance_id])


def signed(payload, key=KEY):
    body = json.dumps(payload)
    timestamp = "1700000000"
    signature = key.sign(timestamp.encode() + body.encode()).signature.hex()
    return {
        "headers": {"x-signature-ed25519": signature, "x-signature-timestamp": timestamp},
        "body": body,
        "isBase64Encoded": False,
    }


def cmd(name, roles):
    payload = {"type": 2, "data": {"name": name}}
    if roles is not None:
        payload["member"] = {"roles": roles}
    return signed(payload)


def call(event):
    return handler.lambda_handler(event, None)


def reply(event):
    response = call(event)
    assert response["statusCode"] == 200
    assert response["headers"] == {"Content-Type": "application/json"}
    body = json.loads(response["body"])
    assert body["type"] == 4
    return body["data"]["content"]


class StubEc2:
    def __init__(self, state, start_error=None, launch_time=None):
        self.state = state
        self.launch_time = launch_time or LAUNCH
        self.start_error = start_error
        self.started = False

    def describe_instances(self, InstanceIds):
        return {"Reservations": [{"Instances": [{"State": {"Name": self.state}, "LaunchTime": self.launch_time}]}]}

    def start_instances(self, InstanceIds):
        if self.start_error:
            raise self.start_error
        self.started = True


class StubSsm:
    def __init__(self, status=None):
        self.status = status
        self.commands = []

    def get_parameter(self, Name):
        if self.status is None:
            raise botocore_error("ParameterNotFound", "GetParameter")
        return {"Parameter": {"Value": json.dumps(self.status)}}

    def send_command(self, **kwargs):
        self.commands.append(kwargs)
        return {"Command": {"CommandId": "c-1"}}


def botocore_error(code, op):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": code}}, op)


@pytest.fixture
def stubs(monkeypatch):
    monkeypatch.setenv("INSTANCE_ID", "i-123")

    def install(ec2_state, status=None, start_error=None, launch_time=None):
        ec2, ssm = StubEc2(ec2_state, start_error, launch_time), StubSsm(status)
        monkeypatch.setattr(handler, "client", lambda name: {"ec2": ec2, "ssm": ssm}[name])
        return ec2, ssm

    return install


# --- protocol ---------------------------------------------------------------

def test_ping():
    assert call(signed({"type": 1}))["body"] == '{"type": 1}'


def test_bad_signature_401():
    event = signed({"type": 1})
    event["body"] = json.dumps({"type": 2})
    assert call(event)["statusCode"] == 401


def test_wrong_key_401():
    assert call(signed({"type": 1}, key=SigningKey.generate()))["statusCode"] == 401


def test_missing_headers_401():
    assert call({"headers": {}, "body": "{}"})["statusCode"] == 401


def test_garbage_signature_401():
    event = signed({"type": 1})
    event["headers"]["x-signature-ed25519"] = "not-hex"
    assert call(event)["statusCode"] == 401


def test_base64_body_is_decoded_before_verification():
    event = signed({"type": 1})
    event["body"] = base64.b64encode(event["body"].encode()).decode()
    event["isBase64Encoded"] = True
    assert call(event)["body"] == '{"type": 1}'


def test_command_reply_flags_zero(stubs):
    stubs("stopped")
    body = json.loads(call(cmd("status", roles=[ZROLE]))["body"])
    assert body["data"]["flags"] == 0


# --- roles ------------------------------------------------------------------

def test_start_requires_zomboid_role():
    event = cmd("start", roles=[])
    assert "zomboid role" in reply(event)
    assert json.loads(call(event)["body"])["data"]["flags"] == 64


def test_status_requires_zomboid_role():
    assert reply(cmd("status", roles=[AROLE])) == "You need the zomboid role to use this command."


def test_dm_without_member_is_refused():
    assert reply(cmd("start", roles=None)) == "You need the zomboid role to use this command."


def test_stop_requires_admin(stubs):
    _, ssm = stubs("running", STATUS_RUNNING)
    event = cmd("stop", roles=[ZROLE])
    assert reply(event) == "Only admins can stop the server."
    assert json.loads(call(event)["body"])["data"]["flags"] == 64
    assert ssm.commands == []


# --- /start -----------------------------------------------------------------

def test_start_starts_stopped_instance(aws):
    ec2, instance_id = aws
    stop_instance(ec2, instance_id)
    assert "Starting the server" in reply(cmd("start", roles=[ZROLE]))
    assert instance_state(ec2, instance_id) in ("pending", "running")


def test_start_reply_text(stubs):
    ec2, _ = stubs("stopped")
    assert reply(cmd("start", roles=[ZROLE])) == "Starting the server. The address comes here in about 3 minutes."
    assert ec2.started


def test_start_while_pending(stubs):
    ec2, _ = stubs("pending")
    assert reply(cmd("start", roles=[ZROLE])) == "The server is already starting. Wait for the address."
    assert not ec2.started


def test_start_while_running_and_status_starting(stubs):
    stubs("running", {"state": "starting", "players": None, "address": None, "session_start": None})
    assert reply(cmd("start", roles=[ZROLE])) == "The server is already starting. Wait for the address."


def test_start_while_running_without_status_parameter(stubs):
    stubs("running", None)
    assert reply(cmd("start", roles=[ZROLE])) == "The server is already starting. Wait for the address."


def test_start_while_running_returns_status(aws):
    put_status(STATUS_RUNNING)
    assert reply(cmd("start", roles=[ZROLE])) == "State: running. Players: 3. Address: 1.2.3.4:16261. Time left: 5h 30m."


def test_start_while_ec2_stopping(stubs):
    ec2, _ = stubs("stopping")
    assert reply(cmd("start", roles=[ZROLE])) == SHUTTING_DOWN
    assert not ec2.started


def test_start_while_running_with_status_stopping(stubs):
    stubs("running", {"state": "stopping", "players": 0, "address": None, "session_start": None, "updated_at": NOW.isoformat()})
    assert reply(cmd("start", roles=[ZROLE])) == SHUTTING_DOWN


def test_start_race_incorrect_instance_state(stubs):
    stubs("stopped", start_error=botocore_error("IncorrectInstanceState", "StartInstances"))
    assert reply(cmd("start", roles=[ZROLE])) == SHUTTING_DOWN


def test_start_stale_stopped_status_means_starting(stubs):
    stale = {"state": "stopped", "updated_at": (LAUNCH - timedelta(hours=1)).isoformat()}
    ec2, _ = stubs("running", stale)
    assert reply(cmd("start", roles=[ZROLE])) == "The server is already starting. Wait for the address."
    assert not ec2.started


def test_start_fresh_stopped_status_means_shutting_down(stubs):
    fresh = {"state": "stopped", "updated_at": (LAUNCH + timedelta(hours=2)).isoformat()}
    ec2, _ = stubs("running", fresh)
    assert reply(cmd("start", roles=[ZROLE])) == SHUTTING_DOWN
    assert not ec2.started


def test_start_stopped_status_at_launch_time_means_shutting_down(stubs):
    stubs("running", {"state": "stopped", "updated_at": LAUNCH.isoformat()})
    assert reply(cmd("start", roles=[ZROLE])) == SHUTTING_DOWN


def test_start_stopped_status_without_updated_at_is_stale(stubs):
    stubs("running", {"state": "stopped"})
    assert reply(cmd("start", roles=[ZROLE])) == "The server is already starting. Wait for the address."


STALE = (LAUNCH - timedelta(hours=1)).isoformat()


def test_start_stale_running_status_means_starting(stubs):
    ec2, _ = stubs("running", dict(STATUS_RUNNING, updated_at=STALE))
    assert reply(cmd("start", roles=[ZROLE])) == "The server is already starting. Wait for the address."
    assert not ec2.started


def test_start_stale_stopping_status_means_starting(stubs):
    ec2, _ = stubs("running", {"state": "stopping", "players": 0, "address": None, "session_start": None, "updated_at": STALE})
    assert reply(cmd("start", roles=[ZROLE])) == "The server is already starting. Wait for the address."
    assert not ec2.started


def test_start_running_status_without_updated_at_means_starting(stubs):
    stubs("running", {k: v for k, v in STATUS_RUNNING.items() if k != "updated_at"})
    assert reply(cmd("start", roles=[ZROLE])) == "The server is already starting. Wait for the address."


@pytest.mark.parametrize("ec2_state", ["shutting-down", "terminated"])
def test_start_while_ec2_going_away(stubs, ec2_state):
    ec2, _ = stubs(ec2_state)
    assert reply(cmd("start", roles=[ZROLE])) == SHUTTING_DOWN
    assert not ec2.started


def test_start_reads_status_once(stubs):
    _, ssm = stubs("running", STATUS_RUNNING)
    reads = []
    original = ssm.get_parameter
    ssm.get_parameter = lambda **kw: reads.append(1) or original(**kw)
    reply(cmd("start", roles=[ZROLE]))
    assert len(reads) == 1


# --- /status ----------------------------------------------------------------

def test_status_text_running(aws):
    put_status(STATUS_RUNNING)
    assert reply(cmd("status", roles=[ZROLE])) == "State: running. Players: 3. Address: 1.2.3.4:16261. Time left: 5h 30m."


def test_status_stopped_omits_address_and_time(aws):
    ec2, instance_id = aws
    stop_instance(ec2, instance_id)
    assert reply(cmd("status", roles=[ZROLE])) == "State: stopped. Players: 0."


def test_status_ignores_stale_running_status_when_ec2_stopped(stubs):
    stubs("stopped", STATUS_RUNNING)
    assert reply(cmd("status", roles=[ZROLE])) == "State: stopped. Players: 0."


def test_status_time_left_floors_at_zero(stubs):
    late = dict(STATUS_RUNNING, session_start=(NOW - timedelta(hours=9)).isoformat())
    stubs("running", late)
    assert reply(cmd("status", roles=[ZROLE])).endswith("Time left: 0h 0m.")


def test_status_time_left_clamps_future_session_start(stubs):
    future = dict(STATUS_RUNNING, session_start=(NOW + timedelta(minutes=5)).isoformat())
    stubs("running", future)
    assert reply(cmd("status", roles=[ZROLE])).endswith("Time left: 8h 0m.")


def test_status_stale_running_status_means_starting(stubs):
    stubs("running", dict(STATUS_RUNNING, updated_at=STALE))
    assert reply(cmd("status", roles=[ZROLE])) == "State: starting. Players: unknown."


def test_status_stale_stopping_status_means_starting(stubs):
    stubs("running", {"state": "stopping", "players": None, "updated_at": STALE})
    assert reply(cmd("status", roles=[ZROLE])) == "State: starting. Players: unknown."


def test_status_starting_without_status_parameter(stubs):
    stubs("pending", None)
    assert reply(cmd("status", roles=[ZROLE])).startswith("State: starting.")


# --- /stop ------------------------------------------------------------------

def test_stop_sends_ssm_command(stubs):
    _, ssm = stubs("running", STATUS_RUNNING)
    assert reply(cmd("stop", roles=[AROLE])) == "Stopping the server. The world is saved first."
    assert ssm.commands == [
        {
            "InstanceIds": ["i-123"],
            "DocumentName": "AWS-RunShellScript",
            "Parameters": {"commands": [STOP_COMMAND]},
        }
    ]


def test_stop_when_not_running(stubs):
    _, ssm = stubs("stopped")
    assert reply(cmd("stop", roles=[AROLE])) == "The server is not running."
    assert ssm.commands == []


def test_stop_with_moto_ssm(aws):
    put_status(STATUS_RUNNING)
    assert reply(cmd("stop", roles=[AROLE])) == "Stopping the server. The world is saved first."


# --- robustness -------------------------------------------------------------

def test_unexpected_exception_becomes_reply(stubs, caplog):
    ec2, _ = stubs("stopped")
    ec2.describe_instances = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    response = call(cmd("start", roles=[ZROLE]))
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["data"]["content"] == "Something went wrong. Try again in one minute."
    assert "boom" in caplog.text


def test_unknown_command(stubs):
    stubs("stopped")
    assert reply(cmd("nope", roles=[ZROLE, AROLE])) == "Unknown command."


def test_no_handler_path_waits(stubs, monkeypatch):
    def fail(*a, **kw):
        raise AssertionError("handler must not wait")

    stubs("running", STATUS_RUNNING)
    monkeypatch.setattr(time, "sleep", fail)
    monkeypatch.setattr(botocore.waiter.Waiter, "wait", fail)
    for name in ("start", "status", "stop"):
        call(cmd(name, roles=[ZROLE, AROLE]))
