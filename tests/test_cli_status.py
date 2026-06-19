"""Tests for the ``status`` CLI command.

The command's job is to answer one question: is the on-disk
``.session`` file accepted by Telegram, right now? It used to lean
on ``start()`` raising on a missing/invalid session; with the
"disconnected mode" change in ``start()``, the command now checks
``is_connected`` itself.

These tests exercise the CLI end-to-end through Typer's
``CliRunner`` with a stubbed ``TelegramService`` so no real network
is touched.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_service(monkeypatch, tmp_path):
    """Patch the helper that ``status`` uses to build a service so
    we can return a stub. Also redirect ``get_settings`` to point at
    a tmp session path with a real on-disk file, so the file-
    existence branch doesn't bail out before we reach the auth
    check."""
    session_file = tmp_path / "session" / "telegram_session.session"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_bytes(b"")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "0" * 32)
    monkeypatch.setenv("TG_PHONE", "+15555550100")
    monkeypatch.setenv("API_KEY", "test-api-key")
    from app.config import get_settings
    get_settings.cache_clear()

    stub = MagicMock()
    stub.is_connected = False  # flipped per-test

    async def _start():
        return None

    async def _stop():
        return None

    stub.start = _start
    stub.stop = _stop

    monkeypatch.setattr("app.cli._build_service", lambda: stub)
    return stub


def test_status_reports_no_session_file(cli_runner, monkeypatch, tmp_path):
    """When the .session file does not exist on disk, the command
    exits 1 with the actionable hint — no service is even built."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "0" * 32)
    monkeypatch.setenv("TG_PHONE", "+15555550100")
    monkeypatch.setenv("API_KEY", "test-api-key")
    from app.config import get_settings
    get_settings.cache_clear()
    (tmp_path / "session").mkdir(parents=True, exist_ok=True)

    result = cli_runner.invoke(cli_app, ["status"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "NO SESSION" in result.output
    assert "dnnotification login" in result.output


def test_status_reports_authorized_when_connected(cli_runner, fake_service):
    """When the service is authorized, exit 0 and print AUTHORIZED."""
    fake_service.is_connected = True
    result = cli_runner.invoke(cli_app, ["status"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "AUTHORIZED" in result.output


def test_status_reports_invalid_when_disconnected(cli_runner, fake_service):
    """When ``start()`` falls into disconnected mode (no env vars /
    prompts), the command must detect that via ``is_connected`` and
    exit 1, instead of falsely reporting AUTHORIZED."""
    fake_service.is_connected = False
    result = cli_runner.invoke(cli_app, ["status"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "SESSION INVALID" in result.output
    assert "dnnotification login" in result.output
