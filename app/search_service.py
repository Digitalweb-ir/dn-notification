from __future__ import annotations

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
    Uses **Telegram's global search** (``client.get_messages(None,
    search=query)``) — a single server-side API call that searches
    across ALL chats at once.  No per-dialog iteration, no local
    message caching.  Results are filtered for exact text match and
    limited to private (user) dialogs only.

    The dialog list (chat IDs + names) is cached with a TTL so we can
    enrich search results with display names without re-listing chats.
    """

    def __init__(self, settings: Settings, telegram: TelegramService) -> None:
        self.settings = settings
        self.telegram = telegram
        self._cache: dict[int, CachedDialog] = {}
        self._cache_ts: float = 0.0

    # ------------------------------------------------------------------ cache

    @property
    def _cache_ttl(self) -> float:
        return float(self.settings.search_cache_ttl)

    def _is_cache_fresh(self) -> bool:
        return (time.time() - self._cache_ts) < self._cache_ttl and bool(self._cache)

    async def _ensure_dialogs_loaded(self) -> None:
        """Refresh the dialog name cache if TTL expired."""
        if self._is_cache_fresh():
            return
        client = self.telegram.require_client()
        started = time.time()
        logger.info("Refreshing dialog list")

        dialogs: list[Dialog] = await self.telegram.safe_call(client.get_dialogs)
        new_cache: dict[int, CachedDialog] = {}
        for d in dialogs:
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

    # ------------------------------------------------------------------ search

    async def search(self, query: str) -> list[SearchResultItem]:
        """Search for an exact message match using Telegram's global search.

        ``client.get_messages(None, search=query)`` sends a single
        ``messages.SearchGlobalRequest`` to Telegram's servers — the
        server does the heavy lifting and returns matching messages
        across all chats.  We then filter for **exact** text match and
        only keep results from private (1-to-1 user) dialogs.
        """
        query = query.strip()
        if not query:
            return []

        await self._ensure_dialogs_loaded()
        client = self.telegram.require_client()
        top_n = self.settings.search_top_matches
        started = time.time()

        # ── ONE API call: global search on the server ──
        msgs = await self.telegram.safe_call(
            client.get_messages,
            None,           # entity=None → SearchGlobalRequest
            search=query,
            limit=50,       # fetch ample results for filtering
        )

        results: list[SearchResultItem] = []
        for m in msgs:
            if len(results) >= top_n:
                break
            if not m or not getattr(m, "message", None):
                continue
            # Exact match — the message text is exactly the query.
            if m.message.strip() != query:
                continue
            # Only private (user) chats.
            peer_id = getattr(m, "peer_id", None)
            chat_id = None
            if peer_id:
                chat_id = getattr(peer_id, "user_id", None)
            if chat_id is None:
                continue
            # Enrich with cached dialog info (name, username).
            dialog = self._cache.get(chat_id)
            results.append(
                SearchResultItem(
                    chat_id=chat_id,
                    username=dialog.username if dialog else None,
                    name=dialog.name if dialog else f"user_{chat_id}",
                    message=m.message,
                    message_date=self._iso(
                        m.date.timestamp() if m.date else 0.0
                    ),
                    message_id=m.id,
                    match_score=1.0,
                )
            )

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
