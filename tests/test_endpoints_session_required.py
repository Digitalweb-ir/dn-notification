"""Endpoint tests covering the "no Telegram session yet" mode.

When the FastAPI lifespan starts without an authorized Telegram
session, the service should:

* start without crashing (so the container does not restart-loop and
  the operator can still run ``dnnotification cli tglogin``);
* answer ``/health`` with ``telegram_connected=false``;
* refuse ``/search`` and ``/send-voice`` with HTTP 503 and an
  actionable detail pointing at the CLI login command.

These tests mount the FastAPI ASGI app directly via httpx with the
lifespan **disabled** — no real Telethon connection is attempted.
The state we'd normally attach in the lifespan
(``app.state.telegram``, etc.) is pre-populated by a fixture.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import _TG_LOGIN_HINT, app


@pytest.fixture(autouse=True)
def _required_settings(monkeypatch, tmp_path):
    """``Settings`` requires Telegram + API key fields. The endpoint
    tests don't need a real session — they only exercise the route
    logic. Set just enough env vars to satisfy the constructor."""
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "0" * 32)
    monkeypatch.setenv("TG_PHONE", "+15555550100")
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "session").mkdir(parents=True, exist_ok=True)
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def disconnected_state():
    """Stub the service-layer objects ``app.state`` would normally
    hold after the lifespan. We do NOT call the real
    ``TelegramService.start()`` here — the lifespan is disabled so
    the real constructor never runs and no network is touched."""
    svc = MagicMock()
    svc.is_connected = False
    svc.client = None
    svc.start = MagicMock()
    svc.stop = MagicMock()
    app.state.telegram = svc
    app.state.search = MagicMock()
    app.state.voice = MagicMock()
    yield svc


@pytest.fixture
async def client(disconnected_state):
    # Use ASGITransport directly so we skip the lifespan entirely —
    # the production code paths are exercised (route handlers,
    # dependency injection) without spinning up the real Telethon
    # client.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_health_reports_disconnected(client):
    """``/health`` must still answer; ``telegram_connected`` is False."""
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["telegram_connected"] is False


@pytest.mark.asyncio
async def test_search_returns_503_when_disconnected(client):
    r = await client.post(
        "/search",
        json={"query": "refund"},
        headers={"X-API-KEY": "test-api-key"},
    )
    assert r.status_code == 503
    assert r.json()["detail"] == _TG_LOGIN_HINT


@pytest.mark.asyncio
async def test_send_voice_returns_503_when_disconnected(client):
    r = await client.post(
        "/send-voice",
        json={"chat_id": 12345, "template": "limited"},
        headers={"X-API-KEY": "test-api-key"},
    )
    assert r.status_code == 503
    assert r.json()["detail"] == _TG_LOGIN_HINT


@pytest.mark.asyncio
async def test_search_requires_api_key_even_when_disconnected(client):
    """Auth check still fires before the session check — we never
    want a missing API key to mask as a login-required 503."""
    r = await client.post("/search", json={"query": "refund"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_send_voice_requires_api_key_even_when_disconnected(client):
    r = await client.post(
        "/send-voice",
        json={"chat_id": 12345, "template": "limited"},
    )
    assert r.status_code == 401
