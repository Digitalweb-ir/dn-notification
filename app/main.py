"""FastAPI entry point for the dn-notification service.

The app exposes a tiny operator surface (``/search`` and ``/send-voice``)
backed by a single Telethon (MTProto) client that is also the source of
truth for the operator CLI's login flow. There is one and only one
``TelegramService`` instance in the process — both the API endpoints
and the admin login endpoints driven by ``app.cli tglogin`` call into
it. After a successful ``tglogin``, ``is_connected`` flips to True on
the same singleton the endpoints already use, so there is no restart,
no handoff, and no second client.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import AsyncIterator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from telethon.tl.types import User

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
# trip the unauthenticated branch. The CLI is now the only path to
# sign in, and the only way to reach it from the host is the passthrough
# `dnnotification cli tglogin`.
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
    dependency translates that state into a 401 with an actionable
    hint. We use 401 (not 503) because the unauthenticated state is
    the operator's "missing credential" condition, parallel to the
    API-key check — and the body is the only signal a non-interactive
    caller gets to understand what is going on.
    """
    telegram: TelegramService = request.app.state.telegram
    if not telegram.is_connected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_TG_LOGIN_HINT,
        )


# --------------------------------------------------------------------- login state


class _LoginState(str, Enum):
    """State machine for the in-flight login driven by admin endpoints.

    Each operator invocation of `dnnotification cli tglogin` gets its
    own LoginSession. The state is advanced by the FastAPI process's
    background task that runs `TelegramService.start(...)` with HTTP-
    driven prompt callbacks; the CLI advances it by submitting values
    via /admin/login/submit and polling /admin/login/status.
    """

    AWAITING_CODE = "awaiting_code"
    AWAITING_PASSWORD = "awaiting_password"
    COMPLETE = "complete"
    FAILED = "failed"


class _LoginSession:
    """In-memory state for one in-flight login.

    The prompt callables passed to `TelegramService.start()` block on
    the ``event`` asyncio.Event until ``submit`` sets it with a value
    submitted by the operator. ``error`` carries a human-readable
    reason if the login failed; ``user`` is the populated Telegram
    user object on success.
    """

    __slots__ = ("session_id", "state", "value", "event", "error", "user", "task")

    def __init__(self, session_id: str, initial_state: _LoginState, task: asyncio.Task) -> None:
        self.session_id = session_id
        self.state: _LoginState = initial_state
        self.value: Optional[str] = None
        self.event: asyncio.Event = asyncio.Event()
        self.error: Optional[str] = None
        self.user: Optional[dict] = None
        self.task: asyncio.Task = task


# Locked to keep /admin/login/start single-flight. _lock on the
# TelegramService already serialises start()/stop(), but we need a
# separate gate to refuse concurrent admin requests with a 409
# before the singleton's start() is even called.
_login_in_progress: Optional[_LoginSession] = None
_login_in_progress_lock = asyncio.Lock()


# --- request/response schemas for the admin surface ---


class LoginStartResponse(BaseModel):
    session_id: str
    state: str


class LoginSubmitRequest(BaseModel):
    session_id: str
    value: str = ""


class LoginStatusResponse(BaseModel):
    session_id: str
    state: str
    user: Optional[dict] = None
    error: Optional[str] = None


class AdminStatusResponse(BaseModel):
    authenticated: bool
    user: Optional[dict] = None
    session_file: str
    session_mtime: Optional[str] = None


def _user_to_dict(me) -> dict:
    if isinstance(me, User):
        return {
            "id": me.id,
            "username": getattr(me, "username", None),
            "first_name": getattr(me, "first_name", None),
            "phone": getattr(me, "phone", None),
        }
    # Fallback: best-effort attribute access for non-User values.
    return {
        "id": getattr(me, "id", None),
        "username": getattr(me, "username", None),
        "first_name": getattr(me, "first_name", None),
        "phone": getattr(me, "phone", None),
    }


async def _run_login(session: _LoginSession, telegram: TelegramService) -> None:
    """Background task that drives `TelegramService.start` for one login.

    The prompt callables block on ``session.event``; the operator
    unblocks them by POSTing to /admin/login/submit. The state
    transitions are driven by the callables themselves (they know
    whether they're being asked for a code or a password) and by
    the success/failure of the underlying Telethon calls.
    """

    async def _code_prompt() -> Optional[str]:
        session.state = _LoginState.AWAITING_CODE
        session.value = None
        session.event.clear()
        # Wait until the operator submits a value via /admin/login/submit.
        # We don't put a timeout on the wait — the operator may take
        # as long as they need to fetch the SMS code.
        await session.event.wait()
        return session.value

    async def _password_prompt() -> Optional[str]:
        session.state = _LoginState.AWAITING_PASSWORD
        session.value = None
        session.event.clear()
        await session.event.wait()
        return session.value

    code_prompt = _code_prompt
    password_prompt = _password_prompt

    try:
        # start() with prompts is the only path that runs the full
        # login flow — calling it from the admin endpoint is exactly
        # what dnnotification cli tglogin used to do in a separate
        # process. The only difference is that this runs in the
        # FastAPI process, against the same TelegramService singleton
        # the endpoints use.
        await telegram.start(
            code_prompt=code_prompt,
            password_prompt=password_prompt,
            max_attempts=None,  # CLI is interactive: keep prompting until right.
        )
        # If start() returned cleanly and the service is connected,
        # capture the user for the response body.
        if telegram.is_connected:
            me = await telegram.require_client().get_me()
            session.user = _user_to_dict(me)
            session.state = _LoginState.COMPLETE
        else:
            # start() returned without raising but did not produce an
            # authorized session. The simplified ``start()`` only
            # reaches this branch if something went wrong — both the
            # lifespan path (no prompts) and the interactive path
            # (prompts supplied) explicitly set ``_connected`` on
            # success. Surface a clear hint and let the operator
            # check the server logs.
            session.error = (
                "Login did not produce an authorized session. "
                "Check the server logs for details."
            )
            session.state = _LoginState.FAILED
    except Exception as exc:  # noqa: BLE001
        # Surface the error verbatim so the CLI can print a clear hint.
        # We do NOT re-raise: the failure must propagate via the
        # /admin/login/{status,submit} response, not crash the
        # FastAPI process.
        logger.exception("Login failed: %s", exc)
        session.error = str(exc) or exc.__class__.__name__
        session.state = _LoginState.FAILED
    finally:
        # Wake up any pending /admin/login/submit call so the CLI
        # can read the final state without waiting for the operator
        # to type something.
        session.event.set()


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
    # endpoint already returns 401 in that case, so warming is moot.
    app.state.warm_task = None
    if telegram.is_connected:

        async def _warm() -> None:
            try:
                await search.refresh_dialogs(force=True)
            except Exception as e:  # noqa: BLE001
                logger.warning("Initial dialog warm-up failed: %s", e)

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


@app.get(
    "/admin/status",
    response_model=AdminStatusResponse,
    dependencies=[Depends(_require_api_key)],
)
async def admin_status(request: Request) -> AdminStatusResponse:
    """Report the current authorization state of the Telegram service.

    Used by the CLI's `status` command and by external monitoring.
    Returns the same shape the operator sees in `/health`, plus a
    `session_file` path and its mtime for the no-auth case (so an
    operator can see whether a session file exists on disk even when
    it isn't yet loaded).
    """
    telegram: TelegramService = request.app.state.telegram
    settings = get_settings()
    session_path = settings.session_path
    user: Optional[dict] = None
    if telegram.is_connected:
        try:
            me = await telegram.require_client().get_me()
            user = _user_to_dict(me)
        except Exception:  # noqa: BLE001
            # Defensive: an authorized client should always be able
            # to return its user. If it can't, fall through to the
            # not-authenticated body shape.
            user = None

    mtime: Optional[str] = None
    if session_path.exists():
        mtime = datetime.fromtimestamp(
            session_path.stat().st_mtime, tz=timezone.utc
        ).isoformat()

    return AdminStatusResponse(
        authenticated=telegram.is_connected,
        user=user,
        session_file=str(session_path),
        session_mtime=mtime,
    )


@app.post(
    "/admin/login/start",
    response_model=LoginStartResponse,
    dependencies=[Depends(_require_api_key)],
)
async def admin_login_start(request: Request) -> LoginStartResponse:
    """Begin an interactive Telegram login in the FastAPI process.

    The login is driven by the running app's own `TelegramService`
    singleton — there is no separate process, no handoff, and no
    restart. The CLI is a thin HTTP client that POSTs the operator's
    code and (if required) 2FA password to `/admin/login/submit`.

    Concurrency: only one in-flight login is allowed at a time. A
    second concurrent call returns 409 until the first one completes
    or fails.
    """
    global _login_in_progress
    telegram: TelegramService = request.app.state.telegram

    # Fast-path: already authorized. Tell the CLI; the operator can
    # decide whether to proceed.
    if telegram.is_connected:
        return LoginStartResponse(
            session_id="",
            state="already_authorized",
        )

    async with _login_in_progress_lock:
        if _login_in_progress is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A login is already in progress. "
                "Submit a value to its session_id or wait for it to fail.",
            )
        session_id = uuid.uuid4().hex
        task = asyncio.create_task(_run_login_starter(session_id, telegram))
        _login_in_progress = _LoginSession(
            session_id=session_id,
            initial_state=_LoginState.AWAITING_CODE,
            task=task,
        )

    return LoginStartResponse(
        session_id=session_id,
        state=_LoginState.AWAITING_CODE.value,
    )


async def _run_login_starter(session_id: str, telegram: TelegramService) -> None:
    """Drive the actual login; clear the singleton slot on completion.

    Wraps `_run_login` so we can keep `_login_in_progress` up to date
    in one place (the finally below), which is the only place we
    can guarantee it gets cleared even if the login raises.
    """
    global _login_in_progress
    session = _login_in_progress
    if session is None or session.session_id != session_id:
        # Defensive: the slot was already cleared (e.g. a concurrent
        # caller was rejected and the lock was reacquired). Nothing
        # to do; the in-progress session, if any, is owned by another
        # task.
        return
    try:
        await _run_login(session, telegram)
    finally:
        async with _login_in_progress_lock:
            # Only clear the slot if it's still ours. A late-arriving
            # concurrent /admin/login/start after this one finished
            # would have observed _login_in_progress != None and
            # returned 409; the slot is ours to clear.
            if _login_in_progress is session:
                _login_in_progress = None


@app.post(
    "/admin/login/submit",
    response_model=LoginStatusResponse,
    dependencies=[Depends(_require_api_key)],
)
async def admin_login_submit(req: LoginSubmitRequest) -> LoginStatusResponse:
    """Submit the operator's code or 2FA password for an in-flight login.

    Empty values are accepted: the operator pressing Enter on an
    empty prompt means "abort", which surfaces as a failed login with
    a clear error in the response body. The CLI uses this same
    channel to drive both the OTP and the 2FA prompts.
    """
    session = _login_in_progress
    if session is None or session.session_id != req.session_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No login is in progress with that session_id.",
        )

    # Empty input signals an explicit abort. Mark the session failed
    # so the CLI can show a clear hint instead of looping on the
    # prompt forever.
    if not req.value:
        session.value = None
        session.error = "Login aborted (empty value submitted)."
        session.state = _LoginState.FAILED
        session.event.set()
        return LoginStatusResponse(
            session_id=session.session_id,
            state=session.state.value,
            user=None,
            error=session.error,
        )

    session.value = req.value
    session.event.set()
    # The state will be advanced by the prompt callable that
    # consumed the value (the code prompt sets AWAITING_CODE->done,
    # and the 2FA prompt sets AWAITING_PASSWORD->done). Wait briefly
    # for the background task to advance the state so the response
    # reflects reality. We don't wait forever — the caller can poll
    # /admin/login/status if this is slow.
    for _ in range(50):  # up to ~5s
        await asyncio.sleep(0.1)
        if session.state in (_LoginState.AWAITING_PASSWORD, _LoginState.COMPLETE, _LoginState.FAILED):
            break

    return LoginStatusResponse(
        session_id=session.session_id,
        state=session.state.value,
        user=session.user,
        error=session.error,
    )


@app.get(
    "/admin/login/status",
    response_model=LoginStatusResponse,
    dependencies=[Depends(_require_api_key)],
)
async def admin_login_status(session_id: str) -> LoginStatusResponse:
    """Poll the state of an in-flight login.

    The submit endpoint already waits for the state to advance, so
    most CLIs don't need to call this — but it's exposed for clients
    that want a non-blocking way to read the current state.
    """
    session = _login_in_progress
    if session is None or session.session_id != session_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No login is in progress with that session_id.",
        )
    return LoginStatusResponse(
        session_id=session.session_id,
        state=session.state.value,
        user=session.user,
        error=session.error,
    )
