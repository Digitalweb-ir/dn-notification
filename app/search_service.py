from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from telethon.tl.custom import Dialog
from telethon.tl.types import User

from .config import Settings
from .logger import get_logger
from .models import SearchResultItem
from .telegram_client import TelegramService

logger = get_logger("search_service")

# Cap concurrent API calls to avoid FloodWait.
_FETCH_SEMAPHORE = asyncio.Semaphore(5)


@dataclass
class CachedDialog:
    """Lightweight dialog metadata — no messages cached."""
    chat_id: int
    username: Optional[str]
    name: str


class SearchService:
    """
    Searches private (1-to-1) dialogs for an exact message match.

    Strategy
    --------
    * The **dialog list** (chat IDs + names) is cached with a TTL so we
      don't re-list chats on every request.
    * Messages are **never** cached locally — that was the old approach
      and it caused 503s because fetching 200 messages × 50 chats takes
      60-100+ seconds.
    * Instead, each search uses Telegram's **server-side search**
      (``client.get_messages(entity, search=query)``) and filters for
      exact text match.  This keeps response times under ~10s even with
      50+ private chats.
    * Once ``search_top_matches`` (default 3) results are found,
      remaining searches are cancelled immediately (early exit).
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
        """Re-fetch the list of private dialogs (fast — no messages)."""
        async with self._lock:
            if self._cache and not force and self._is_cache_fresh():
                return

            client = self.telegram.require_client()
            started = time.time()
            logger.info("Refreshing dialog list%s", " (forced)" if force else "")

            dialogs: list[Dialog] = await self.telegram.safe_call(client.get_dialogs)
            private_dialogs = [d for d in dialogs if d.is_user]

            new_cache: dict[int, CachedDialog] = {}
            for d in private_dialogs:
                entity = d.entity
                if not isinstance(entity, User) or entity.bot or entity.deleted:
                    continue
                new_cache[entity.id] = CachedDialog(
                    chat_id=entity.id,
                    username=entity.username,
                    name=self._display_name(entity),
                )

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

    # ------------------------------------------------------------------ search

    async def search(self, query: str) -> list[SearchResultItem]:
        """Search for an exact message match across all private dialogs.

        Uses Telegram's server-side search (``get_messages(search=…)``)
        per dialog, then filters for **exact** text match.  Returns at
        most ``search_top_matches`` (default 3) results.

        Early exit: as soon as enough results are found, remaining
        in-flight searches are cancelled.
        """
        query = query.strip()
        if not query:
            return []

        await self._ensure_dialogs_loaded()
        client = self.telegram.require_client()
        top_n = self.settings.search_top_matches
        results: list[SearchResultItem] = []
        started = time.time()

        async def _search_dialog(
            chat_id: int, dialog: CachedDialog
        ) -> Optional[SearchResultItem]:
            try:
                async with _FETCH_SEMAPHORE:
                    msgs = await self.telegram.safe_call(
                        client.get_messages,
                        chat_id,
                        search=query,
                        limit=5,
                    )
            except Exception:  # noqa: BLE001
                logger.warning("Search failed for chat %s", chat_id)
                return None

            for m in msgs:
                if not m or not getattr(m, "message", None):
                    continue
                # Exact match — the message text is exactly the query.
                if m.message.strip() == query:
                    return SearchResultItem(
                        chat_id=dialog.chat_id,
                        username=dialog.username,
                        name=dialog.name,
                        message=m.message,
                        message_date=self._iso(
                            m.date.timestamp() if m.date else 0.0
                        ),
                        message_id=m.id,
                        match_score=1.0,
                    )
            return None

        # Fire off all dialog searches concurrently (capped by the
        # semaphore), but stop as soon as we have enough results.
        pending: set[asyncio.Task] = {
            asyncio.create_task(_search_dialog(cid, d))
            for cid, d in self._cache.items()
        }

        while pending and len(results) < top_n:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                r = task.result()
                if r is not None:
                    results.append(r)

        # Cancel any remaining in-flight searches — we have enough.
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        results.sort(key=lambda r: r.message_date, reverse=True)
        logger.info(
            "Search '%s': %d result(s) in %.1fs",
            query,
            len(results),
            time.time() - started,
        )
        return results[:top_n]

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
