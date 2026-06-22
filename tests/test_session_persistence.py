"""Tests for session persistence after a successful Telegram login.

The CLI's ``tglogin`` creates a TelegramClient directly, performs login,
and saves the session to disk. These tests verify that the session file
is properly written and reusable by a fresh TelegramService.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import User as TgUser

from app.config import Settings
from app.telegram_client import TelegramService


# --- helpers -----------------------------------------------------------------


def _build_service(tmp_path) -> TelegramService:
    """Build a TelegramService with a tmp session dir."""
    settings = Settings(
        tg_api_id=12345,
        tg_api_hash="0123456789abcdef0123456789abcdef",
        tg_phone="+15555550100",
        api_key="test-api-key",
        data_dir=str(tmp_path),
        tg_session_name="test",
    )
    (tmp_path / "session").mkdir(parents=True, exist_ok=True)
    return TelegramService(settings)


def _make_real_session_client(*, authorized_after_signin: bool = True):
    """Build a TelegramClient stand-in that uses a REAL SQLiteSession."""
    from telethon.sessions import SQLiteSession

    class RealSessionClient:
        def __init__(self, session_path, *args, **kwargs):
            self.session = SQLiteSession(str(session_path))
            self._sender = MagicMock()
            self._sender.auth_key = self.session._auth_key
            self._authorized = self.session._auth_key is not None

        async def connect(self):
            if self._sender.auth_key is None:
                from telethon.crypto import AuthKey
                new_key = AuthKey(data=b"\x02" * 256)
                self._sender.auth_key = new_key
                self.session.auth_key = new_key
                self.session.save()
            return True

        async def is_user_authorized(self):
            return self._authorized

        async def send_code_request(self, phone):
            return MagicMock(phone_code_hash="hash")

        async def sign_in(self, *args, code=None, password=None, **kwargs):
            if password is not None:
                self._authorized = authorized_after_signin
                return None
            raise SessionPasswordNeededError(request=None)

        async def disconnect(self):
            try:
                self.session.close()
            except Exception:
                pass
            return True

        async def get_me(self):
            return MagicMock(
                spec=TgUser,
                id=999,
                first_name="Tester",
                username="tester",
                phone="+15555550100",
            )

    return RealSessionClient


# --- tests -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_file_exists_after_successful_login(tmp_path, monkeypatch):
    """After the CLI's ``tglogin`` saves a session, the on-disk
    .session file MUST exist and contain a non-empty ``auth_key`` blob."""
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)

    session_path = tmp_path / "session" / "test.session"
    assert not session_path.exists(), "precondition: no session file"

    # Ensure session directory exists (CLI tglogin would have it
    # created by docker-entrypoint.sh; tests need it manually).
    (tmp_path / "session").mkdir(parents=True, exist_ok=True)

    # Simulate what the CLI does: create a TelegramClient, connect,
    # login, disconnect.
    RealClient = _make_real_session_client()
    monkeypatch.setattr("app.telegram_client.TelegramClient", RealClient)

    client = RealClient(str(session_path), 12345, "0" * 32)
    await client.connect()
    # Simulate OTP + 2FA sign_in
    try:
        await client.sign_in("+15555550100", "11111")
    except SessionPasswordNeededError:
        await client.sign_in(password="secret")
    await client.disconnect()

    assert session_path.exists(), (
        f"login reported success but {session_path} was not created"
    )

    conn = sqlite3.connect(str(session_path))
    try:
        cursor = conn.execute("select length(auth_key) from sessions")
        (auth_key_length,) = cursor.fetchone()
    finally:
        conn.close()

    assert auth_key_length == 256, (
        f"session file was created but auth_key is "
        f"{auth_key_length} bytes — expected 256."
    )


@pytest.mark.asyncio
async def test_session_reusable_by_telegram_service(tmp_path, monkeypatch):
    """The .session file written by ``tglogin`` MUST be usable by
    ``TelegramService.start()`` — no interactive prompt needed."""
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)

    session_path = tmp_path / "session" / "test.session"
    RealClient = _make_real_session_client()
    monkeypatch.setattr("app.telegram_client.TelegramClient", RealClient)

    # Ensure session directory exists.
    (tmp_path / "session").mkdir(parents=True, exist_ok=True)

    # Simulate CLI tglogin: create client, login, disconnect
    client = RealClient(str(session_path), 12345, "0" * 32)
    await client.connect()
    try:
        await client.sign_in("+15555550100", "11111")
    except SessionPasswordNeededError:
        await client.sign_in(password="secret")
    await client.disconnect()

    # Now build a TelegramService that points at the same session.
    # start() must NOT prompt for anything — the on-disk auth_key
    # is enough.
    fresh_svc = _build_service(tmp_path)
    monkeypatch.setattr("app.telegram_client.TelegramClient", RealClient)
    await fresh_svc.start()

    assert fresh_svc.is_connected is True


@pytest.mark.asyncio
async def test_session_file_written_even_when_connect_runs(tmp_path, monkeypatch):
    """``connect()`` alone (before sign_in) writes the auth_key to disk.
    Even if the operator aborts before completing login, the session
    file should be present."""
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)

    session_path = tmp_path / "session" / "test.session"
    RealClient = _make_real_session_client()
    monkeypatch.setattr("app.telegram_client.TelegramClient", RealClient)

    # Ensure session directory exists.
    (tmp_path / "session").mkdir(parents=True, exist_ok=True)

    # Simulate CLI connecting but aborting before sign_in
    client = RealClient(str(session_path), 12345, "0" * 32)
    await client.connect()
    # Simulate abort: disconnect without signing in
    await client.disconnect()

    assert session_path.exists(), (
        "session file should be on disk after connect() even if "
        "the operator aborts before completing login"
    )
