"""Tests for the ``status`` CLI command.

The CLI's status command is a thin HTTP client: it calls
``GET /admin/status`` on the running FastAPI process and prints the
result. These tests exercise the CLI end-to-end through Typer's
``CliRunner`` while patching the HTTP layer so no real network or
TelegramService is touched. The hint strings the operator sees
point at ``dnnotification cli tglogin`` (the only sign-in path).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def _settings(monkeypatch, tmp_path):
    """Settings needs API_KEY for the CLI to authenticate to the
    admin endpoints; set just enough env vars to satisfy the
    constructor. The HTTP layer is patched per-test, so the CLI
    never actually opens a socket."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "0" * 32)
    monkeypatch.setenv("TG_PHONE", "+15555550100")
    monkeypatch.setenv("API_KEY", "test-api-key")
    (tmp_path / "session").mkdir(parents=True, exist_ok=True)
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_response(status_code: int, body: dict) -> MagicMock:
    """Build a minimal httpx.Response stand-in for the patched
    request helper. The CLI only reads .status_code and .json()."""
    r = MagicMock()
    r.status_code = status_code
    r.json = MagicMock(return_value=body)
    r.text = str(body)
    return r


def test_status_reports_unauthorized_when_admin_says_so(cli_runner, _settings):
    """When /admin/status returns ``authenticated: false``, the CLI
    exits 1 and prints the actionable hint pointing at
    ``dnnotification cli tglogin`` (NOT the obsolete
    ``dnnotification login``)."""
    response = _make_response(
        200,
        {
            "authenticated": False,
            "user": None,
            "session_file": "/var/lib/dn-notification/session/test.session",
            "session_mtime": None,
        },
    )
    with patch("app.cli._request", AsyncMock(return_value=response)):
        result = cli_runner.invoke(cli_app, ["status"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "NOT AUTHORIZED" in result.output
    assert "dnnotification cli tglogin" in result.output


def test_status_reports_authorized_when_admin_says_so(cli_runner, _settings):
    """When /admin/status returns ``authenticated: true`` with a
    user, the CLI exits 0 and prints the user info."""
    response = _make_response(
        200,
        {
            "authenticated": True,
            "user": {
                "id": 999,
                "username": "tester",
                "first_name": "Tester",
                "phone": "+15555550100",
            },
            "session_file": "/var/lib/dn-notification/session/test.session",
            "session_mtime": "2026-06-19T10:00:00+00:00",
        },
    )
    with patch("app.cli._request", AsyncMock(return_value=response)):
        result = cli_runner.invoke(cli_app, ["status"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "AUTHORIZED" in result.output
    assert "tester" in result.output
    assert "/var/lib/dn-notification/session/test.session" in result.output


def test_status_propagates_unreachable_service(cli_runner, _settings):
    """If the running service is unreachable, the CLI surfaces a
    clear hint (the wrapper of the HTTP helper turns httpx errors
    into SystemExit with a message) and exits 1."""
    async def _raise(*_args, **_kwargs):
        # Mirror what the real ``_request`` helper does when the
        # underlying httpx call fails: translate the ``httpx`` error
        # into a ``SystemExit`` carrying the actionable hint.
        raise SystemExit(
            "Could not reach the running service at http://127.0.0.1:8000 "
            "(ConnectError: connection refused). Is the container up? "
            "Try `dnnotification up` or `docker logs dn-notification`."
        )

    with patch("app.cli._request", _raise):
        result = cli_runner.invoke(cli_app, ["status"], catch_exceptions=False)
    assert result.exit_code == 1
    # The hint mentions both the URL and what to do.
    assert "127.0.0.1:8000" in result.output
    assert "container" in result.output.lower()
