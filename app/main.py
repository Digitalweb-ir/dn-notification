from __future__ import annotations

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
