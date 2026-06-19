"""FastAPI entry point for the dn-notification service."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from . import __version__
from .config import get_settings
from .logger import get_logger, setup_logging
from .models import (
    HealthResponse,
    SearchRequest,
    SearchResponse,
    SendVoiceRequest,
    SendVoiceResponse,
)
from .search_service import SearchService
from .telegram_client import TelegramService, get_telegram_service
from .voice_service import VoiceService, VoiceServiceError

logger = get_logger("main")


# Message returned by every endpoint that needs an authorized Telegram
# session. Centralised so the operator-facing text stays consistent —
# the wording is the only actionable hint the user gets when they
# trip the unauthenticated branch.
_TG_LOGIN_HINT = (
    "Telegram session is not available. "
    "Run `dnnotification login` to sign in."
)


# --------------------------------------------------------------------- auth


def _require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-KEY")) -> None:
    settings = get_settings()
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-KEY header",
        )


def _require_telegram_session(request: Request) -> None:
    """Refuse endpoints that need an authorized Telegram session.

    The lifespan starts the service in disconnected mode when no
    session file is on disk (so the container does not crash-loop and
    the operator can still run `dnnotification cli tglogin`). This
    dependency translates that state into a 503 with an actionable
    hint, instead of letting the underlying Telethon call surface an
    opaque auth error.
    """
    telegram: TelegramService = request.app.state.telegram
    if not telegram.is_connected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_TG_LOGIN_HINT,
        )


# --------------------------------------------------------------------- app


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    settings = get_settings()
    logger.info("Starting Telegram automation service")

    telegram: TelegramService = get_telegram_service()
    await telegram.start()

    search = SearchService(settings, telegram)
    voice = VoiceService(settings, telegram)

    # Warm the dialog cache in the background so the first request is
    # fast. Skip when the lifespan started in disconnected mode (no
    # authorized session) — ``refresh_dialogs`` would just fail, and
    # ``require_client`` would raise on the empty client state. The
    # warm task will be a no-op until ``tglogin`` is run; the search
    # endpoint already returns 503 in that case, so warming is moot.
    app.state.warm_task = None
    if telegram.is_connected:

        async def _warm() -> None:
            try:
                await search.refresh_dialogs(force=True)
            except Exception as e:  # noqa: BLE001
                logger.warning("Initial dialog warm-up failed: %s", e)

        import asyncio
        app.state.warm_task = asyncio.create_task(_warm())

    app.state.telegram = telegram
    app.state.search = search
    app.state.voice = voice

    try:
        yield
    finally:
        logger.info("Shutting down")
        task = getattr(app.state, "warm_task", None)
        if task and not task.done():
            task.cancel()
        await telegram.stop()


app = FastAPI(
    title="Telegram Automation API",
    description=(
        "Personal-account Telegram automation (MTProto/Telethon) for support workflows. "
        "Searches private dialogs and sends voice notes."
    ),
    version=__version__,
    lifespan=lifespan,
)


# --------------------------------------------------------------------- routes


@app.exception_handler(VoiceServiceError)
async def _voice_error_handler(_: Request, exc: VoiceServiceError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    telegram: TelegramService = get_telegram_service()
    return HealthResponse(
        status="ok",
        telegram_connected=telegram.is_connected,
        session=get_settings().tg_session_name,
    )


@app.post(
    "/search",
    response_model=SearchResponse,
    dependencies=[
        Depends(_require_api_key),
        Depends(_require_telegram_session),
    ],
)
async def search(req: SearchRequest, request: Request) -> SearchResponse:
    svc: SearchService = request.app.state.search
    results = await svc.search(req.query)
    return SearchResponse(query=req.query, count=len(results), results=results)


@app.post(
    "/send-voice",
    response_model=SendVoiceResponse,
    dependencies=[
        Depends(_require_api_key),
        Depends(_require_telegram_session),
    ],
)
async def send_voice(req: SendVoiceRequest, request: Request) -> SendVoiceResponse:
    svc: VoiceService = request.app.state.voice
    return await svc.send(req.chat_id, req.template)


# --------------------------------------------------------------------- admin


@app.post(
    "/admin/handoff",
    dependencies=[Depends(_require_api_key)],
)
async def admin_handoff(request: Request) -> JSONResponse:
    """Reload the TelegramService singleton from the on-disk .session.

    The CLI's ``tglogin`` command runs in a separate process and owns
    its own ``TelegramClient``. After it completes sign-in, the
    authorized auth_key is on disk under ``$DATA_DIR/session/`` but
    the running service's in-memory client — built at startup when no
    session existed — is stale. The CLI POSTs here, and we stop the
    stale client and start a fresh one that loads the auth_key from
    the same file. Because the CLI's sign_in authorized the auth_key
    server-side, the new client's ``is_user_authorized()`` returns
    True and endpoints like ``/search`` stop returning 503.

    The ``_lock`` on ``TelegramService`` already serializes start()
    and stop(), so the handoff is naturally race-free against
    concurrent requests.

    Idempotent: a second handoff when already connected is a no-op.
    """
    telegram: TelegramService = request.app.state.telegram
    if telegram.is_connected:
        return JSONResponse(
            status_code=200,
            content={"ok": True, "already_connected": True},
        )

    try:
        await telegram.stop()
        await telegram.start()
    except Exception as exc:  # noqa: BLE001
        # The handoff is best-effort: the on-disk .session is still
        # valid even if we can't reach Telegram from the running
        # process right now (network blip, etc.). The next container
        # restart will pick it up. Surface the error so the CLI can
        # log a clear hint instead of silently failing.
        logger.exception("Handoff failed: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(exc)},
        )

    if not telegram.is_connected:
        # start() returned without raising, but the on-disk session
        # wasn't authorized (e.g. another process is in the middle of
        # writing the auth_key). Tell the CLI to retry.
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": "Service is still disconnected after reload. "
                "The on-disk session may be incomplete; re-run "
                "`dnnotification login`.",
            },
        )

    me = await telegram.require_client().get_me()
    logger.info(
        "Handoff complete — service is now authorized as %s",
        getattr(me, "username", None) or getattr(me, "first_name", "?"),
    )
    return JSONResponse(
        status_code=200,
        content={"ok": True, "already_connected": False},
    )
