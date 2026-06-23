"""FastAPI entry point for the dn-notification service.

The app exposes REST endpoints backed by a single Telethon (MTProto)
client.  There is one and only one ``TelegramService`` instance in the
process — API endpoints call into it, and the service auto-detects
session files written by ``tglogin`` (which runs in a separate CLI
process).

Login is performed by the CLI (``python -m app.cli tglogin``) in a
separate process. After the CLI saves the session file, the next API
request triggers ``ensure_connected()``, which reconnects using the
on-disk session — no admin endpoints needed.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from . import __version__
from .config import get_settings
from .logger import get_logger, setup_logging
from .message_service import MessageService, MessageServiceError
from .models import (
    HealthResponse,
    SendMessageRequest,
    SendMessageResponse,
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
# trip the unauthenticated branch. The CLI is the only path to sign in.
_TG_LOGIN_HINT = (
    "Telegram session is not available. "
    "Run `dnnotification cli tglogin` to sign in."
)


# --------------------------------------------------------------------- auth


def _require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-KEY")) -> None:
    settings = get_settings()
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-KEY header",
        )


async def _require_telegram_session(request: Request) -> None:
    """Refuse endpoints that need an authorized Telegram session.

    The lifespan starts the service in disconnected mode when no
    session file is on disk (so the container does not crash-loop and
    the operator can still run ``dnnotification cli tglogin``). This
    dependency translates that state into a 401 with an actionable
    hint — but first tries ``ensure_connected()`` so that a session
    file written by the CLI is picked up automatically.

    We use 401 (not 503) because the unauthenticated state is the
    operator's "missing credential" condition, parallel to the API-key
    check — and the body is the only signal a non-interactive caller
    gets to understand what is going on.
    """
    telegram: TelegramService = request.app.state.telegram
    if not telegram.is_connected:
        # Auto-reconnect: if the CLI's tglogin wrote a session file
        # since the service started, pick it up now.
        reconnected = await telegram.ensure_connected()
        if not reconnected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=_TG_LOGIN_HINT,
            )


# --------------------------------------------------------------------- app


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    settings = get_settings()
    logger.info(
        "Starting Telegram automation service (log_level=%s, debug=%s)",
        settings.log_level,
        settings.debug,
    )

    telegram: TelegramService = get_telegram_service()
    await telegram.start()

    search = SearchService(settings, telegram)
    voice = VoiceService(settings, telegram)
    message = MessageService(settings, telegram)

    app.state.telegram = telegram
    app.state.search = search
    app.state.voice = voice
    app.state.message = message

    try:
        yield
    finally:
        logger.info("Shutting down")
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


@app.exception_handler(MessageServiceError)
async def _message_error_handler(_: Request, exc: MessageServiceError) -> JSONResponse:
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


@app.post(
    "/send-message",
    response_model=SendMessageResponse,
    dependencies=[
        Depends(_require_api_key),
        Depends(_require_telegram_session),
    ],
)
async def send_message(req: SendMessageRequest, request: Request) -> SendMessageResponse:
    svc: MessageService = request.app.state.message
    return await svc.send(req.chat_id, req.shortcut)
