"""Slack configuration for setup.

Alerts go out through chat.postMessage, and the optional deal rundown through
the canvases API. Both need a bot token, a channel, and the right scopes —
and every one of those fails in a way that is obvious to Slack and opaque to
the user, so this translates each into something actionable.

The channel is asked for rather than picked from a list on purpose.
conversations.list requires channels:read, groups:read, im:read and mpim:read
together — the last two let an app enumerate direct messages, which is far
more access than a deal notifier should hold just to render a menu. A pasted
channel id is validated instead by posting one real message, which proves the
channel exists, the bot can reach it, and chat:write is granted, using the
exact call the scanner itself makes.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import dataclass, field

from hd.logging import get_logger

log = get_logger("setup_slack")

AUTH_TEST_URL = "https://slack.com/api/auth.test"
POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"

SCOPE_ALERTS = "chat:write"
SCOPE_CANVAS = "canvases:write"

# Slack error codes translated into the thing the user must actually do. The
# raw codes are accurate but unhelpful: "not_in_channel" does not tell anyone
# to invite the bot.
_ERROR_HELP = {
    "invalid_auth": "Slack rejected that token. Copy the Bot User OAuth Token, which starts with xoxb-.",
    "account_inactive": "That token belongs to a deactivated app or workspace.",
    "token_revoked": "That token has been revoked. Reinstall the app and copy the new one.",
    "not_authed": "No token was sent.",
    "channel_not_found": "No channel with that id. Copy the id from the channel's About tab, e.g. C0123456789.",
    "not_in_channel": "The bot is not in that channel. In Slack, run /invite @your-app in the channel, then retry.",
    "is_archived": "That channel is archived.",
    "missing_scope": "The app is missing a required scope. Add it in OAuth & Permissions and reinstall.",
    "restricted_action": "Workspace settings prevent the app from posting there.",
}


class SlackSetupError(RuntimeError):
    """A Slack step failed, with a message worth showing the user."""


@dataclass(frozen=True)
class SlackIdentity:
    """Who a token belongs to, and what it is allowed to do."""

    team: str
    bot_name: str
    scopes: set[str] = field(default_factory=set)

    def missing(self, *required: str) -> list[str]:
        """Required scopes this token does not hold.

        An empty scopes set means Slack did not report them, in which case
        nothing is claimed missing rather than guessing wrong.
        """
        if not self.scopes:
            return []
        return [s for s in required if s not in self.scopes]


def explain(code: str) -> str:
    """Turn a Slack error code into an instruction."""
    return _ERROR_HELP.get(code, f"Slack returned '{code}'.")


async def _call(token: str, url: str, payload: dict) -> tuple[dict, dict[str, str]]:
    """POST to Slack, returning (body, headers).

    Headers matter here: Slack reports a token's granted scopes in
    x-oauth-scopes, which is the only way to check for canvases:write without
    creating a canvas as a side effect of asking.
    """
    cmd = [
        "curl", "-s", "-D", "-", "-o", "-",
        "-X", "POST",
        "-H", "Content-Type: application/json; charset=utf-8",
        "-H", f"Authorization: Bearer {token}",
        "-d", json.dumps(payload),
        "--max-time", "15",
        url,
    ]
    cmd_str = " ".join(shlex.quote(p) for p in cmd)

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd_str, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError as exc:
        raise SlackSetupError("Slack did not respond within 20s") from exc

    raw = stdout.decode(errors="replace")
    head, _, body = raw.partition("\r\n\r\n")
    if not body:
        head, _, body = raw.partition("\n\n")

    headers: dict[str, str] = {}
    for line in head.splitlines():
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()

    try:
        data = json.loads(body.strip())
    except json.JSONDecodeError as exc:
        raise SlackSetupError("Slack returned a response that could not be read") from exc

    return data, headers


def _parse_scopes(headers: dict[str, str]) -> set[str]:
    raw = headers.get("x-oauth-scopes", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


async def verify_token(token: str) -> SlackIdentity:
    """Confirm a bot token works and report who it is.

    Showing the workspace and bot name back is the cheapest way for someone to
    notice they pasted a token for the wrong workspace.
    """
    token = token.strip()
    if not token:
        raise SlackSetupError("A bot token is required")
    if not token.startswith("xoxb-"):
        log.info("Token does not look like a bot token", prefix=token[:5])

    data, headers = await _call(token, AUTH_TEST_URL, {})
    if not data.get("ok"):
        raise SlackSetupError(explain(str(data.get("error", "unknown"))))

    return SlackIdentity(
        team=str(data.get("team") or "unknown workspace"),
        bot_name=str(data.get("user") or "unknown bot"),
        scopes=_parse_scopes(headers),
    )


async def send_test_message(token: str, channel_id: str, text: str) -> str:
    """Post one message, proving the channel and chat:write both work.

    Returns the message timestamp. Raises with an actionable message when
    Slack refuses — most often because the bot was never invited.
    """
    channel_id = channel_id.strip()
    if not channel_id:
        raise SlackSetupError("A channel id is required")

    data, _ = await _call(
        token, POST_MESSAGE_URL, {"channel": channel_id, "text": text, "unfurl_links": False}
    )
    if not data.get("ok"):
        raise SlackSetupError(explain(str(data.get("error", "unknown"))))
    return str(data.get("ts") or "")
