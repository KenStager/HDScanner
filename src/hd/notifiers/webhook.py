"""POST formatted alerts to an OpenClaw webhook via curl subprocess."""

from __future__ import annotations

import asyncio
import json
import shlex

from hd.config import Settings
from hd.logging import get_logger

log = get_logger("notifiers.webhook")


async def post_to_openclaw(settings: Settings, message: str) -> bool:
    """Send a Slack message via OpenClaw webhook. Returns True on success."""
    url = settings.openclaw_webhook_url
    if not url:
        log.warning("openclaw_webhook_url is not configured")
        return False

    body = {
        "message": message,
        "deliver": True,
        "channel": "slack",
    }
    if settings.slack_channel_id:
        body["to"] = settings.slack_channel_id

    body_json = json.dumps(body)

    cmd_parts = [
        "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
        "-X", "POST",
        "-H", "Content-Type: application/json",
    ]
    if settings.openclaw_token:
        cmd_parts.extend(["-H", f"x-openclaw-token: {settings.openclaw_token}"])
    cmd_parts.extend(["-d", body_json, "--max-time", "10", url])

    cmd_str = " ".join(shlex.quote(p) for p in cmd_parts)

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd_str,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        status_code = stdout.decode().strip()

        if status_code.startswith("2"):
            log.info("Webhook delivered", status=status_code)
            return True
        else:
            log.warning("Webhook failed", status=status_code, stderr=stderr.decode().strip())
            return False
    except asyncio.TimeoutError:
        log.warning("Webhook timed out")
        return False
    except Exception as exc:
        log.warning("Webhook error", error=str(exc))
        return False
