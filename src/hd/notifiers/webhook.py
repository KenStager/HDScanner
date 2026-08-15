"""POST formatted alerts to Slack via chat.postMessage API."""

from __future__ import annotations

import asyncio
import json
import shlex

from hd.config import Settings
from hd.logging import get_logger

log = get_logger("notifiers.webhook")

SLACK_API_URL = "https://slack.com/api/chat.postMessage"


async def post_to_openclaw(
    settings: Settings,
    message: str,
    blocks: list[dict] | None = None,
) -> bool:
    """Send a Slack message via chat.postMessage. Returns True on success."""
    token = settings.slack_bot_token
    if not token:
        log.warning("SLACK_BOT_TOKEN is not configured")
        return False

    channel = settings.slack_channel_id
    if not channel:
        log.warning("SLACK_CHANNEL_ID is not configured")
        return False

    payload: dict = {
        "channel": channel,
        "text": message,
        "unfurl_links": False,
    }
    if blocks:
        payload["blocks"] = blocks

    body = json.dumps(payload)

    cmd_parts = [
        "curl", "-s", "-w", "\n%{http_code}",
        "-X", "POST",
        "-H", "Content-Type: application/json; charset=utf-8",
        "-H", f"Authorization: Bearer {token}",
        "-d", body,
        "--max-time", "10",
        SLACK_API_URL,
    ]

    cmd_str = " ".join(shlex.quote(p) for p in cmd_parts)

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd_str,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        output = stdout.decode().strip()
        lines = output.rsplit("\n", 1)
        response_body = lines[0] if len(lines) > 1 else ""
        status_code = lines[-1]

        if status_code.startswith("2"):
            # Check Slack API ok field
            try:
                result = json.loads(response_body)
                if result.get("ok"):
                    log.info("Slack message delivered", channel=channel)
                    return True
                else:
                    log.warning("Slack API error", error=result.get("error", "unknown"))
                    return False
            except json.JSONDecodeError:
                log.warning("Slack response not JSON", body=response_body[:200])
                return False
        else:
            log.warning("Slack POST failed", status=status_code, stderr=stderr.decode().strip())
            return False
    except asyncio.TimeoutError:
        log.warning("Slack POST timed out")
        return False
    except Exception as exc:
        log.warning("Slack POST error", error=str(exc))
        return False
