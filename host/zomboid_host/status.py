import json
from datetime import datetime, timezone

PARAMETER = "/zomboid/status"


def write_status(
    ssm,
    state: str,
    players: int | None,
    address: str | None,
    session_start: datetime | None,
) -> None:
    body = {
        "state": state,
        "players": players,
        "address": address,
        "session_start": session_start.isoformat() if session_start else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    ssm.put_parameter(Name=PARAMETER, Value=json.dumps(body), Type="String", Overwrite=True)


def read_status(ssm) -> dict:
    try:
        resp = ssm.get_parameter(Name=PARAMETER)
    except ssm.exceptions.ParameterNotFound:
        return {"state": "stopped"}
    return json.loads(resp["Parameter"]["Value"])
