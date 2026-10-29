import json
import logging
import urllib.request

log = logging.getLogger(__name__)


def post(webhook_url: str, content: str) -> None:
    """Post a message to a Discord webhook. Errors are logged, never raised."""
    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps({"content": content}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "zomboid-host/0.1"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        log.exception("Discord post failed")
