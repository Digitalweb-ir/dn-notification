"""Tests for the ``/admin/handoff`` endpoint.

The CLI's ``tglogin`` command runs in a separate process and writes
a fresh auth_key to the bind-mounted ``.session`` file. The running
FastAPI process has its own in-memory Telethon client that was
constructed at startup (when no session existed), so it is stale
even though the on-disk file is now valid. The handoff endpoint
reloads the singleton against the on-disk file and flips
``is_connected`` to True, so ``/search`` and ``/send-voice`` stop
returning 503 without a container restart.

These tests mount the FastAPI ASGI app directly via httpx with the
lifespan **disabled** — no real Telethon connection is attempted.
The ``TelegramService`` singleton is replaced with a stub that
records the calls and lets each test pin down a specific behavior.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture(autouse=True)
def _required_settings(monkeypatch, tmp_path):
    """``Settings`` requires Telegram + API key fields. Set just
    enough env vars to satisfy the constructor — the handoff test
    does not exercise the real ``TelegramService``."""
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


class _StubTelegramService:
    """Stand-in for ``TelegramService`` used by the handoff test.

    Records the order of ``stop()`` and ``start()`` calls so a test
    can assert the handoff endpoint called them in the right
    sequence, and lets the test control the post-start
    ``is_connected`` value to simulate success / failure paths.
    """

    def __init__(self, *, post_start_connected: bool = True):
        self.is_connected = False
        self.client = None
        self._post_start_connected = post_start_connected
        self.calls: list[str] = []
        # The lock used by the real TelegramService. The handoff
        # endpoint does not interact with it directly, but other
        # tests may set it up so we provide a no-op stand-in.
        self._lock = AsyncMock()
        self._lock.__aenter__ = AsyncMock(return_value=None)
        self._lock.__aexit__ = AsyncMock(return_value=None)

    async def stop(self) -> None:
        self.calls.append("stop")
        # stop() tears down the client; mirror the real one.
        self.client = None

    async def start(self) -> None:
        self.calls.append("start")
        # Real start() will (in the happy case) flip _connected to
        # True after a successful is_user_authorized() call.
        self.is_connected = self._post_start_connected
        if self._post_start_connected:
            me = MagicMock()
            me.username = "tester"
            me.first_name = "Tester"
            me.id = 999
            me.phone = "+15555550100"
            self.client = MagicMock()
            self.client.get_me = AsyncMock(return_value=me)

    def require_client(self):
        # The handoff endpoint calls require_client().get_me() to
        # log the post-handoff user; mirror the real interface.
        if self.client is None:
            raise RuntimeError("client not initialized")
        return self.client


@pytest.fixture
def handoff_service():
    """Attach a fresh stub service to ``app.state`` for each test."""
    svc = _StubTelegramService()
    app.state.telegram = svc

    # Wire async stubs for search/voice. The default MagicMock
    # returns coroutine mocks for ``await`` calls but its methods
    # return MagicMocks when called directly — which is what the
    # ``/search`` and ``/send-voice`` endpoints need to look
    # realistic here. Use AsyncMock so the endpoints' ``await
    # svc.search(...)`` works after a successful handoff.
    search = MagicMock()
    search.search = AsyncMock(return_value=[])
    app.state.search = search
    voice = MagicMock()
    voice.send = AsyncMock(return_value=None)
    app.state.voice = voice
    yield svc


@pytest.fixture
async def client(handoff_service):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --- tests ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_requires_api_key(client):
    """No ``X-API-KEY`` header → 401. Same auth contract as the
    other operator endpoints, so the handoff is not a back door
    into the running service."""
    r = await client.post("/admin/handoff")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_handoff_rejects_wrong_api_key(client):
    r = await client.post(
        "/admin/handoff", headers={"X-API-KEY": "not-the-right-one"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_handoff_reloads_service_when_disconnected(client, handoff_service):
    """Happy path: the service starts in disconnected mode, the
    CLI posts here after writing a fresh ``.session`` file, and the
    endpoint reloads the service. After the reload,
    ``is_connected`` is True and the endpoint returns 200."""
    assert handoff_service.is_connected is False
    handoff_service._post_start_connected = True

    r = await client.post(
        "/admin/handoff", headers={"X-API-KEY": "test-api-key"}
    )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body.get("already_connected") is False
    assert handoff_service.is_connected is True
    # Order matters: stop the stale client, then start the new one.
    assert handoff_service.calls == ["stop", "start"]


@pytest.mark.asyncio
async def test_handoff_is_idempotent_when_already_connected(
    client, handoff_service
):
    """If the running service is already authorized (e.g. a
    second ``dnnotification login`` invocation while the first is
    still good), the handoff is a no-op — no stop/start cycle."""
    handoff_service.is_connected = True
    handoff_service._post_start_connected = True

    r = await client.post(
        "/admin/handoff", headers={"X-API-KEY": "test-api-key"}
    )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body.get("already_connected") is True
    # Crucially: no stop/start happened, so the existing client
    # was not torn down.
    assert handoff_service.calls == []


@pytest.mark.asyncio
async def test_handoff_returns_500_when_start_raises(client, handoff_service):
    """If ``start()`` raises (e.g. the on-disk session is invalid
    or the network is unreachable), the handoff returns 500 and
    ``is_connected`` stays False. The endpoint does NOT swallow
    the exception — the CLI needs to know the handoff did not
    succeed so it can print a clear hint."""
    handoff_service.is_connected = False
    handoff_service._post_start_connected = False

    # Make start() raise. We patch it on the instance, not the
    # class, so the test stays local.
    boom = RuntimeError("Telegram rejected the auth_key")
    async def _raise():
        handoff_service.calls.append("start")
        raise boom
    handoff_service.start = _raise  # type: ignore[method-assign]

    r = await client.post(
        "/admin/handoff", headers={"X-API-KEY": "test-api-key"}
    )

    assert r.status_code == 500
    body = r.json()
    assert body["ok"] is False
    assert "Telegram rejected the auth_key" in body["error"]
    # is_connected is still False (the stub's start() raised
    # before flipping it). The on-disk session is still valid
    # from the CLI's perspective; the CLI will print a warning
    # suggesting `dnnotification restart`.
    assert handoff_service.is_connected is False
    assert handoff_service.calls == ["stop", "start"]


@pytest.mark.asyncio
async def test_handoff_returns_503_when_start_succeeds_but_still_disconnected(
    client, handoff_service
):
    """Defensive: if start() returned without raising, but the
    service is still disconnected (e.g. another process is in the
    middle of writing the auth_key on disk), the handoff returns
    503 with a hint. The CLI surfaces this to the operator."""
    handoff_service._post_start_connected = False

    r = await client.post(
        "/admin/handoff", headers={"X-API-KEY": "test-api-key"}
    )

    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert "re-run" in body["error"].lower()


@pytest.mark.asyncio
async def test_search_becomes_live_after_handoff(client, handoff_service):
    """End-to-end: while the service is disconnected, ``/search``
    returns 503. After the handoff, the same request returns 200.

    This is the user-visible promise of the fix — the operator
    logs in via the CLI, the running service picks up the new
    session, and endpoints that needed a session stop returning
    503. The 200/empty-results response is the observable
    difference."""
    from app.main import _TG_LOGIN_HINT

    # 1) Pre-handoff: /search is 503 with the login hint.
    pre = await client.post(
        "/search",
        json={"query": "hello"},
        headers={"X-API-KEY": "test-api-key"},
    )
    assert pre.status_code == 503
    assert pre.json()["detail"] == _TG_LOGIN_HINT

    # 2) The CLI posts to the handoff endpoint. The stub's
    # start() flips is_connected to True and the search service
    # (a MagicMock) is wired to a real httpx-style request that
    # the dependency container resolves to app.state.search.
    handoff_service._post_start_connected = True
    handoff = await client.post(
        "/admin/handoff", headers={"X-API-KEY": "test-api-key"}
    )
    assert handoff.status_code == 200

    # 3) Post-handoff: /search no longer 503s. The actual
    # response body comes from the MagicMock search service, but
    # what matters here is the status code — it dropped from
    # 503 (disconnected) to 200 (live).
    post = await client.post(
        "/search",
        json={"query": "hello"},
        headers={"X-API-KEY": "test-api-key"},
    )
    assert post.status_code == 200, (
        f"expected /search to be live after handoff, got "
        f"{post.status_code} {post.text}"
    )
