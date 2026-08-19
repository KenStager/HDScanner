"""Tests for Slack setup.

Slack's error codes are precise and useless to a person: "not_in_channel" does
not tell anyone to invite the bot. What is pinned here is that each failure
becomes an instruction, and that a scope Slack never reported is not claimed
to be missing.
"""

from __future__ import annotations

import pytest

from hd import setup_slack as ss
from hd.setup_slack import (
    SCOPE_ALERTS,
    SCOPE_CANVAS,
    SlackIdentity,
    SlackSetupError,
    _parse_scopes,
    explain,
    send_test_message,
    verify_token,
)


def _call(body: dict, headers: dict | None = None):
    async def _inner(token, url, payload):
        return body, headers or {}
    return _inner


class TestVerifyToken:
    async def test_returns_identity_and_scopes(self, monkeypatch):
        monkeypatch.setattr(ss, "_call", _call(
            {"ok": True, "team": "Acme", "user": "dealbot"},
            {"x-oauth-scopes": "chat:write,canvases:write"},
        ))
        who = await verify_token("xoxb-abc")
        assert who.team == "Acme" and who.bot_name == "dealbot"
        assert who.scopes == {"chat:write", "canvases:write"}

    async def test_invalid_auth_becomes_an_instruction(self, monkeypatch):
        monkeypatch.setattr(ss, "_call", _call({"ok": False, "error": "invalid_auth"}))
        with pytest.raises(SlackSetupError) as exc:
            await verify_token("xoxb-bad")
        assert "xoxb-" in str(exc.value)

    async def test_blank_token_rejected_without_a_request(self, monkeypatch):
        async def _boom(*a, **k):
            raise AssertionError("should not call Slack for an empty token")
        monkeypatch.setattr(ss, "_call", _boom)
        with pytest.raises(SlackSetupError):
            await verify_token("   ")

    async def test_unknown_error_still_surfaces(self, monkeypatch):
        monkeypatch.setattr(ss, "_call", _call({"ok": False, "error": "weird_thing"}))
        with pytest.raises(SlackSetupError) as exc:
            await verify_token("xoxb-abc")
        assert "weird_thing" in str(exc.value)


class TestSendTestMessage:
    async def test_success_returns_timestamp(self, monkeypatch):
        monkeypatch.setattr(ss, "_call", _call({"ok": True, "ts": "1699999999.000100"}))
        assert await send_test_message("xoxb", "C123", "hi") == "1699999999.000100"

    async def test_not_in_channel_tells_the_user_to_invite(self, monkeypatch):
        """The most common Slack setup failure by far."""
        monkeypatch.setattr(ss, "_call", _call({"ok": False, "error": "not_in_channel"}))
        with pytest.raises(SlackSetupError) as exc:
            await send_test_message("xoxb", "C123", "hi")
        assert "/invite" in str(exc.value)

    async def test_channel_not_found_explains_where_to_look(self, monkeypatch):
        monkeypatch.setattr(ss, "_call", _call({"ok": False, "error": "channel_not_found"}))
        with pytest.raises(SlackSetupError) as exc:
            await send_test_message("xoxb", "nope", "hi")
        assert "About tab" in str(exc.value)

    async def test_blank_channel_rejected(self):
        with pytest.raises(SlackSetupError):
            await send_test_message("xoxb", "  ", "hi")


class TestScopes:
    def test_parses_header(self):
        assert _parse_scopes({"x-oauth-scopes": "chat:write, canvases:write"}) == {
            "chat:write", "canvases:write"
        }

    def test_missing_reports_absent_scope(self):
        who = SlackIdentity("Acme", "bot", {"chat:write"})
        assert who.missing(SCOPE_CANVAS) == [SCOPE_CANVAS]
        assert who.missing(SCOPE_ALERTS) == []

    def test_unreported_scopes_claim_nothing(self):
        """Slack does not always send the header; silence is not absence."""
        who = SlackIdentity("Acme", "bot", set())
        assert who.missing(SCOPE_ALERTS, SCOPE_CANVAS) == []


class TestExplain:
    def test_known_codes_are_actionable(self):
        for code in ("not_in_channel", "channel_not_found", "invalid_auth", "missing_scope"):
            assert explain(code) and not explain(code).startswith("Slack returned")

    def test_unknown_code_is_passed_through(self):
        assert "mystery" in explain("mystery")


class TestCanvasToggle:
    """The wizard writes CANVAS_ENABLED, so the setting must actually exist.

    Settings uses extra="ignore", so an unknown key would be dropped in
    silence and the canvas would keep running after the user declined it.
    """

    def test_setting_exists_and_defaults_on(self):
        from hd.config import Settings

        assert Settings().canvas_enabled is True

    def test_env_value_is_honoured(self, monkeypatch):
        from hd.config import Settings

        monkeypatch.setenv("CANVAS_ENABLED", "false")
        assert Settings().canvas_enabled is False

    async def test_disabled_canvas_does_no_work(self, monkeypatch):
        """Must short-circuit before querying deals or calling Slack."""
        from hd.config import Settings
        from hd.notifiers import canvas as cv

        async def _boom(*a, **k):
            raise AssertionError("canvas ran while disabled")

        monkeypatch.setattr(cv, "get_active_deals", _boom)
        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:", canvas_enabled=False
        )
        assert await cv.run_canvas_update(settings) == ("", 0)
