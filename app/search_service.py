from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from telethon.tl.custom import Dialog
from telethon.tl.types import User

from .config import Settings
from .logger import get_logger
from .models import SearchResultItem
from .telegram_client import TelegramService

logger = get_logger("search_service")

# Cap concurrent get_messages calls so we don't flood Telegram's API
# and hit FloodWait.  5 concurrent ≈ reasonable without being abusive.
_FETCH_SEMAPHORE = asyncio.Semaphore(5)


@dataclass
class CachedMessage:
    message_id: int
    text: str
    date_ts: float  # unix timestamp (float)
    chat_id: int


@dataclass
class CachedDialog:
    chat_id: int
    username: Optional[str]
    name: str
    messages: list[CachedMessage] = field(default_factory=list)
    last_message_ts: float = 0.0


class SearchService:
    """
    Scans private (1-to-1) dialogs for a keyword.

    Strategy:
      * After startup, the dialog list is fetched once and cached (TTL).
      * On every search we iterate the cached dialogs and pull the most
        recent N messages per chat. We then run a scored substring match.
      * To avoid the cost of re-fetching unchanged chats on every call,
        we only re-fetch chats whose last message timestamp is older than
        what we have stored (or if we have nothing for that chat).
    """

    def __init__(self, settings: Settings, telegram: TelegramService) -> None:
        self.settings = settings
        self.telegram = telegram
        self._cache: dict[int, CachedDialog] = {}
        self._cache_ts: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ cache

    @property
    def _cache_ttl(self) -> float:
        return float(self.settings.search_cache_ttl)

    def _is_cache_fresh(self) -> bool:
        return (time.time() - self._cache_ts) < self._cache_ttl and bool(self._cache)

    async def refresh_dialogs(self, force: bool = False) -> None:
        """Re-fetch all private dialogs. Slow; only call on startup or force."""
        async with self._lock:
            client = self.telegram.require_client()
            started = time.time()
            logger.info("Refreshing private dialog cache%s", " (forced)" if force else "")

            dialogs: list[Dialog] = await self.telegram.safe_call(client.get_dialogs)
            private_dialogs = [d for d in dialogs if d.is_user]

            # Build the dialog map first (no API calls for messages yet).
            new_cache: dict[int, CachedDialog] = {}
            entries: list[tuple[int, CachedDialog]] = []
            for d in private_dialogs:
                entity = d.entity
                if not isinstance(entity, User) or entity.bot or entity.deleted:
                    continue

                chat_id = entity.id
                username = entity.username
                name = self._display_name(entity)
                cached = CachedDialog(chat_id=chat_id, username=username, name=name)
                new_cache[chat_id] = cached
                entries.append((chat_id, cached))

            # Fetch messages for every dialog concurrently (capped by
            # _FETCH_SEMAPHORE).  This is the expensive part — doing it
            # sequentially made the endpoint take 60-100+ seconds with
            # 50+ private chats, causing HTTP 503 timeouts.
            async def _fetch_one(
                chat_id: int, dialog: CachedDialog
            ) -> None:
                try:
                    async with _FETCH_SEMAPHORE:
                        msgs = await self.telegram.safe_call(
                            client.get_messages,
                            chat_id,
                            limit=self.settings.search_limit_per_chat,
                        )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Skip chat %s during refresh (fetch failed)", chat_id
                    )
                    return

                for m in msgs:
                    if not m or not getattr(m, "message", None):
                        continue
                    dialog.messages.append(
                        CachedMessage(
                            message_id=m.id,
                            text=m.message,
                            date_ts=m.date.timestamp() if m.date else 0.0,
                            chat_id=chat_id,
                        )
                    )
                if dialog.messages:
                    dialog.last_message_ts = max(
                        m.date_ts for m in dialog.messages
                    )

            if entries:
                await asyncio.gather(*[_fetch_one(cid, d) for cid, d in entries])

            self._cache = new_cache
            self._cache_ts = time.time()
            logger.info(
                "Cached %d private dialogs in %.1fs",
                len(self._cache),
                time.time() - started,
            )

    async def _ensure_dialogs_loaded(self) -> None:
        if not self._is_cache_fresh():
            await self.refresh_dialogs(force=True)

    async def _refresh_stale_dialogs(self) -> None:
        """For each cached dialog, pull newer messages if any exist."""
        if not self._cache:
            await self.refresh_dialogs(force=True)
            return

        client = self.telegram.require_client()

        async def _refresh_one(chat_id: int, dialog: CachedDialog) -> None:
            try:
                async with _FETCH_SEMAPHORE:
                    msgs = await self.telegram.safe_call(
                        client.get_messages, chat_id, limit=20
                    )
            except Exception:  # noqa: BLE001
                logger.warning("Skip chat %s during refresh", chat_id)
                return

            latest_in_cache = dialog.last_message_ts
            new_msgs: list[CachedMessage] = []
            for m in msgs:
                if not m or not getattr(m, "message", None):
                    continue
                ts = m.date.timestamp() if m.date else 0.0
                if ts > latest_in_cache:
                    new_msgs.append(
                        CachedMessage(
                            message_id=m.id,
                            text=m.message,
                            date_ts=ts,
                            chat_id=chat_id,
                        )
                    )

            if new_msgs:
                dialog.messages = (
                    new_msgs + dialog.messages
                )[: self.settings.search_limit_per_chat]
                dialog.last_message_ts = max(
                    m.date_ts for m in dialog.messages
                ) if dialog.messages else 0.0

        async with self._lock:
            entries = list(self._cache.items())
            if entries:
                await asyncio.gather(
                    *[_refresh_one(cid, d) for cid, d in entries]
                )

            self._cache_ts = time.time()

    # ------------------------------------------------------------------ search

    async def search(self, query: str) -> list[SearchResultItem]:
        query = query.strip()
        if not query:
            return []

        await self._ensure_dialogs_loaded()
        await self._refresh_stale_dialogs()

        query_lower = query.lower()
        top_n = self.settings.search_top_matches

        results: list[SearchResultItem] = []
        for dialog in self._cache.values():
            scored: list[tuple[float, CachedMessage]] = []
            for msg in dialog.messages:
                score = self._score(query_lower, msg.text)
                if score > 0:
                    scored.append((score, msg))

            if not scored:
                continue

            # highest score first, then most recent
            scored.sort(key=lambda x: (x[0], x[1].date_ts), reverse=True)
            for score, msg in scored[:top_n]:
                results.append(
                    SearchResultItem(
                        chat_id=dialog.chat_id,
                        username=dialog.username,
                        name=dialog.name,
                        message=msg.text,
                        message_date=self._iso(msg.date_ts),
                        message_id=msg.message_id,
                        match_score=round(score, 4),
                    )
                )

        # Sort final result by most recent match
        results.sort(key=lambda r: r.message_date, reverse=True)
        return results

    # ------------------------------------------------------------------ utils

    @staticmethod
    def _display_name(user: User) -> str:
        parts = [user.first_name or "", user.last_name or ""]
        name = " ".join(p for p in parts if p).strip()
        if not name:
            name = user.username or f"user_{user.id}"
        return name

    @staticmethod
    def _iso(ts: float) -> str:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    @staticmethod
    def _score(query: str, text: str) -> float:
        """
        Cheap scoring function:
          * Exact substring match => 1.0
          * Word-boundary match   => 0.7
          * Subsequence match     => 0.3
          * No match              => 0
        Length of message is used to slightly penalize very long ones.
        """
        if not text:
            return 0.0
        t = text.lower()
        if query in t:
            if f" {query} " in f" {t} " or t.startswith(query) or t.endswith(query):
                base = 0.9
            else:
                base = 0.6
            # Penalize long messages
            return max(0.1, base - min(0.4, len(text) / 1000.0))

        # Subsequence match
        i = 0
        for ch in t:
            if ch == query[i]:
                i += 1
                if i == len(query):
                    return 0.25
        return 0.0
