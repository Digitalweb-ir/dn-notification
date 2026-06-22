"""Tests for the ``status`` CLI command.

The CLI's status command checks the session file locally and queries
the running service's ``/health`` endpoint. These tests exercise the
CLI end-to-end through Typer's ``CliRunner`` while patching the HTTP
layer and filesystem so no real network or TelegramService is touched.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def _settings(monkeypatch, tmp_path):
    """Set enough env vars to satisfy the Settings constructor.
    The HTTP layer is patched per-test, so the CLI never actually opens
    a socket."""
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


def _make_httpx_response(status_code: int, body: dict) -> MagicMock:
    """Build a minimal httpx.Response stand-in for the patched client."""
    r = MagicMock()
    r.status_code = status_code
    r.json = MagicMock(return_value=body)
    r.text = json.dumps(body)
    return r


def test_status_reports_authorized_when_service_connected(cli_runner, _settings):
    """When /health returns ``telegram_connected: true``, the CLI
    exits 0 and prints AUTHORIZED."""
    response = _make_httpx_response(
        200,
        {
            "status": "ok",
            "telegram_connected": True,
            "session": "test",
        },
    )

    # Patch httpx.AsyncClient so we don't make real HTTP calls
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.cli.httpx.AsyncClient", return_value=mock_client):
        result = cli_runner.invoke(cli_app, ["status"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "AUTHORIZED" in result.output


def test_status_reports_not_connected_when_service_disconnected(
    cli_runner, _settings,
):
    """When /health returns ``telegram_connected: false``, the CLI
    exits 1 and prints an actionable hint."""
    response = _make_httpx_response(
        200,
        {
            "status": "ok",
            "telegram_connected": False,
            "session": "test",
        },
    )

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.cli.httpx.AsyncClient", return_value=mock_client):
        result = cli_runner.invoke(cli_app, ["status"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "NOT CONNECTED" in result.output
    assert "dnnotification cli tglogin" in result.output


def test_status_handles_unreachable_service(cli_runner, _settings):
    """If the running service is unreachable, the CLI surfaces a clear
    hint and exits 1."""
    import httpx

    async def _raise(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=_raise)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.cli.httpx.AsyncClient", return_value=mock_client):
        result = cli_runner.invoke(cli_app, ["status"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "Could not reach" in result.output


def test_status_shows_session_file_info(cli_runner, _settings, tmp_path):
    """The status command prints the session file path and its mtime
    when a session file exists on disk."""
    # The default session name is "telegram_session", so the file
    # is at tmp_path/session/telegram_session.session.
    session_path = tmp_path / "session" / "telegram_session.session"
    # Write a minimal file so it shows up with mtime
    session_path.write_bytes(b"")

    response = _make_httpx_response(
        200,
        {"status": "ok", "telegram_connected": True, "session": "test"},
    )

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.cli.httpx.AsyncClient", return_value=mock_client):
        result = cli_runner.invoke(cli_app, ["status"], catch_exceptions=False)
    assert "Session file" in result.output
    assert "Last modified" in result.output
