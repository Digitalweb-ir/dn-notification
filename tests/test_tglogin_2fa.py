"""Unit tests for the Telegram login flow and TelegramService.

Tests cover:
- Password prompt preserves whitespace (2FA password must not be stripped)
- `start()` does not construct a TelegramClient when no session file exists
- `start()` reuses a valid session file when one exists
- `ensure_connected()` auto-reconnects when a session file appears on disk
- `ensure_connected()` returns False when no session file exists
- `ensure_connected()` returns True when already connected

The tests run against the real ``TelegramService`` with a mocked Telethon
client — no network access.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl.types import User as TgUser

from app.config import Settings
from app.telegram_client import TelegramService


# --- helpers -----------------------------------------------------------------


def _build_service(tmp_path, monkeypatch) -> TelegramService:
    """Build a TelegramService backed by a tmp session dir."""
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


def _make_client_mock(*, is_authorized=False):
    """Build a mock TelegramClient."""
    client = AsyncMock()
    client.connect = AsyncMock(return_value=True)
    client.is_user_authorized = AsyncMock(return_value=is_authorized)
    client.disconnect = AsyncMock(return_value=True)
    me = MagicMock(
        spec=TgUser,
        id=999,
        first_name="Tester",
        username="tester",
        phone="+15555550100",
    )
    client.get_me = AsyncMock(return_value=me)
    return client


class _FakeTelegramClient:
    """Drop-in replacement for ``telethon.TelegramClient`` used by tests."""

    instances: list = []  # type: ignore[type-arg]

    def __init__(self, *args, **kwargs):
        if not hasattr(self, "_mock"):
            self._mock = AsyncMock()
        self._mock.connect = AsyncMock(return_value=True)
        self._mock.is_user_authorized = AsyncMock(return_value=False)
        self._mock.disconnect = AsyncMock(return_value=True)
        me = MagicMock(
            spec=TgUser,
            id=999,
            first_name="Tester",
            username="tester",
            phone="+15555550100",
        )
        self._mock.get_me = AsyncMock(return_value=me)
        _FakeTelegramClient.instances.append(self)

    def __getattr__(self, name):
        return getattr(self._mock, name)


@pytest.fixture(autouse=True)
def _patch_telegram_client_constructor(monkeypatch):
    """Replace ``TelegramClient`` in the telegram_client module with a
    fake that doesn't try to connect to Telegram."""
    import app.telegram_client as tc_mod

    monkeypatch.setattr(tc_mod, "TelegramClient", _FakeTelegramClient)
    _FakeTelegramClient.instances = []
    yield


# --- tests -------------------------------------------------------------------


def test_password_prompt_preserves_whitespace(monkeypatch):
    """``_prompt_password`` must not strip — Telegram 2FA passwords may
    legitimately contain leading/trailing whitespace that contributes
    to the hash."""
    from app.cli import _prompt_password

    monkeypatch.setattr(
        "app.cli.getpass.getpass", lambda *a, **kw: "  spaced pass  "
    )
    assert _prompt_password() == "  spaced pass  "


def test_prompt_code_strips_whitespace(monkeypatch):
    """``_prompt_code`` should strip — SMS codes are digits-only."""
    from app.cli import _prompt_code

    monkeypatch.setattr(
        "app.cli.getpass.getpass", lambda *a, **kw: "  11111  "
    )
    assert _prompt_code() == "11111"


@pytest.mark.asyncio
async def test_start_does_not_construct_client_when_no_session(
    tmp_path, monkeypatch
):
    """The lifespan path (``start()``) must NOT construct a
    ``TelegramClient`` when there is no authorized session on disk.

    This is the fix for the "client disconnects immediately after login"
    bug. Skipping client construction in the lifespan means exactly one
    ``TelegramClient`` is constructed per login, eliminating the race.
    """
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path, monkeypatch)

    class _ExplodingTelegramClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "TelegramClient must NOT be constructed during the "
                "lifespan path when there is no authorized session."
            )

    import app.telegram_client as tc_mod
    monkeypatch.setattr(tc_mod, "TelegramClient", _ExplodingTelegramClient)

    # Must not raise — that was the original crash-loop bug.
    await svc.start()

    assert svc.is_connected is False
    assert svc.client is None


@pytest.mark.asyncio
async def test_start_succeeds_when_session_already_authorized(
    tmp_path, monkeypatch
):
    """When a valid session file exists, ``start()`` must connect,
    detect ``is_user_authorized()`` returns True, and set
    ``is_connected=True`` without going through the login flow."""
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path, monkeypatch)

    # Plant a populated .session file.
    session_path = tmp_path / "session" / "test.session"
    import sqlite3
    with sqlite3.connect(str(session_path)) as conn:
        conn.execute(
            "CREATE TABLE sessions (auth_key BLOB, user_id INTEGER, "
            "is_authorized INTEGER)"
        )
        conn.execute(
            "INSERT INTO sessions (auth_key, user_id, is_authorized) "
            "VALUES (?, ?, ?)",
            (b"\x02" * 256, 999, 1),
        )
        conn.commit()

    fake = _FakeTelegramClient.__new__(_FakeTelegramClient)
    fake._mock = AsyncMock()
    fake._mock.connect = AsyncMock(return_value=True)
    fake._mock.is_user_authorized = AsyncMock(return_value=True)
    fake._mock.disconnect = AsyncMock(return_value=True)
    me = MagicMock(
        spec=TgUser,
        id=999, first_name="Tester", username="tester", phone="+15555550100"
    )
    fake._mock.get_me = AsyncMock(return_value=me)

    import app.telegram_client as tc_mod
    instances = [fake]

    class _Queue:
        def __call__(self, *args, **kwargs):
            return instances.pop(0)

    monkeypatch.setattr(tc_mod, "TelegramClient", _Queue())

    await svc.start()

    assert svc.is_connected is True
    assert svc.client is fake


@pytest.mark.asyncio
async def test_ensure_connected_returns_true_when_already_connected(
    tmp_path, monkeypatch
):
    """``ensure_connected()`` must return True immediately if the
    service is already connected — no reconnect attempt."""
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path, monkeypatch)

    # Simulate connected state
    fake = _FakeTelegramClient.__new__(_FakeTelegramClient)
    fake._mock = AsyncMock()
    fake._mock.disconnect = AsyncMock(return_value=True)
    svc.client = fake
    svc._connected = True

    # Plant a session file so _has_authorized_session_file would return
    # True — but the early return should prevent any re-check.
    session_path = tmp_path / "session" / "test.session"
    import sqlite3
    with sqlite3.connect(str(session_path)) as conn:
        conn.execute(
            "CREATE TABLE sessions (auth_key BLOB)"
        )
        conn.execute(
            "INSERT INTO sessions (auth_key) VALUES (?)",
            (b"\x02" * 256,),
        )
        conn.commit()

    result = await svc.ensure_connected()
    assert result is True
    assert fake._mock.connect.call_count == 0  # no reconnect attempted


@pytest.mark.asyncio
async def test_ensure_connected_returns_false_when_no_session(
    tmp_path, monkeypatch
):
    """``ensure_connected()`` must return False when no session file
    exists on disk — there is nothing to reconnect with."""
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path, monkeypatch)

    # No session file, not connected
    assert svc.is_connected is False

    class _ExplodingTelegramClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "TelegramClient must NOT be constructed when there is "
                "no session file."
            )

    import app.telegram_client as tc_mod
    monkeypatch.setattr(tc_mod, "TelegramClient", _ExplodingTelegramClient)

    result = await svc.ensure_connected()
    assert result is False
    assert svc.is_connected is False
    assert svc.client is None


@pytest.mark.asyncio
async def test_ensure_connected_succeeds_when_session_appeared(
    tmp_path, monkeypatch
):
    """Regression: when the CLI's ``tglogin`` writes a session file
    while the FastAPI app is running (disconnected),
    ``ensure_connected()`` must detect the new file, construct a
    TelegramClient, and set ``is_connected=True``."""
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path, monkeypatch)

    # Start with no session — the service is disconnected.
    await svc.start()
    assert svc.is_connected is False
    assert svc.client is None

    # Plant a session file (simulates CLI tglogin completing).
    session_path = tmp_path / "session" / "test.session"
    import sqlite3
    with sqlite3.connect(str(session_path)) as conn:
        conn.execute(
            "CREATE TABLE sessions (auth_key BLOB, user_id INTEGER, "
            "is_authorized INTEGER)"
        )
        conn.execute(
            "INSERT INTO sessions (auth_key, user_id, is_authorized) "
            "VALUES (?, ?, ?)",
            (b"\x02" * 256, 999, 1),
        )
        conn.commit()

    fake = _FakeTelegramClient.__new__(_FakeTelegramClient)
    fake._mock = AsyncMock()
    fake._mock.connect = AsyncMock(return_value=True)
    fake._mock.is_user_authorized = AsyncMock(return_value=True)
    fake._mock.disconnect = AsyncMock(return_value=True)
    me = MagicMock(
        spec=TgUser,
        id=999, first_name="Tester", username="tester", phone="+15555550100"
    )
    fake._mock.get_me = AsyncMock(return_value=me)

    import app.telegram_client as tc_mod
    instances = [fake]

    class _Queue:
        def __call__(self, *args, **kwargs):
            return instances.pop(0)

    monkeypatch.setattr(tc_mod, "TelegramClient", _Queue())

    result = await svc.ensure_connected()
    assert result is True
    assert svc.is_connected is True


@pytest.mark.asyncio
async def test_ensure_connected_handles_reconnect_failure(
    tmp_path, monkeypatch
):
    """``ensure_connected()`` must return False (not crash) if
    ``start()`` raises — e.g. the session file exists but Telethon
    cannot connect."""
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path, monkeypatch)

    # Plant a session file so _has_authorized_session_file returns True.
    session_path = tmp_path / "session" / "test.session"
    import sqlite3
    with sqlite3.connect(str(session_path)) as conn:
        conn.execute(
            "CREATE TABLE sessions (auth_key BLOB)"
        )
        conn.execute(
            "INSERT INTO sessions (auth_key) VALUES (?)",
            (b"\x02" * 256,),
        )
        conn.commit()

    fake = _FakeTelegramClient.__new__(_FakeTelegramClient)
    fake._mock = AsyncMock()
    fake._mock.connect = AsyncMock(side_effect=ConnectionError("network down"))
    fake._mock.disconnect = AsyncMock(return_value=True)

    import app.telegram_client as tc_mod
    instances = [fake]

    class _Queue:
        def __call__(self, *args, **kwargs):
            return instances.pop(0)

    monkeypatch.setattr(tc_mod, "TelegramClient", _Queue())

    result = await svc.ensure_connected()
    assert result is False


@pytest.mark.asyncio
async def test_client_stays_connected_after_lifespan_reconnect(
    tmp_path, monkeypatch
):
    """After the lifespan detects a session and connects, the client
    must stay connected — no silent disconnect."""
    import asyncio

    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path, monkeypatch)

    # Plant a session file.
    session_path = tmp_path / "session" / "test.session"
    import sqlite3
    with sqlite3.connect(str(session_path)) as conn:
        conn.execute(
            "CREATE TABLE sessions (auth_key BLOB, user_id INTEGER)"
        )
        conn.execute(
            "INSERT INTO sessions (auth_key, user_id) VALUES (?, ?)",
            (b"\x02" * 256, 999),
        )
        conn.commit()

    fake = _FakeTelegramClient.__new__(_FakeTelegramClient)
    fake._mock = AsyncMock()
    fake._mock.connect = AsyncMock(return_value=True)
    fake._mock.is_user_authorized = AsyncMock(return_value=True)
    fake._mock.disconnect = AsyncMock(return_value=True)
    me = MagicMock(
        spec=TgUser,
        id=999, first_name="Tester", username="tester", phone="+15555550100"
    )
    fake._mock.get_me = AsyncMock(return_value=me)

    import app.telegram_client as tc_mod
    instances = [fake]

    class _Queue:
        def __call__(self, *args, **kwargs):
            return instances.pop(0)

    monkeypatch.setattr(tc_mod, "TelegramClient", _Queue())

    await svc.start()
    assert svc.is_connected is True
    assert svc.client is fake

    # The client must stay connected across event loop ticks.
    await asyncio.sleep(0)
    assert svc.is_connected is True
    assert fake._mock.disconnect.call_count == 0
