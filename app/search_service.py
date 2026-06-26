from __future__ import annotations

import time
from typing import Optional

from telethon.tl import functions, types
from telethon.tl.types import User

from .config import Settings
from .logger import get_logger
from .models import SearchResultItem
from .telegram_client import TelegramService

logger = get_logger("search_service")


class SearchService:
    """Searches private (1-to-1) dialogs for an exact message match.

    Strategy
    --------
    Calls ``messages.SearchGlobalRequest`` **directly** (bypassing
    Telethon's ``get_messages`` wrapper) with ``users_only=True`` so
    the Telegram server only searches user-to-user chats — no
    channels, groups, or bots.  This is a single API call, no
    pagination, no dialog cache needed.

    Results are filtered for **exact** text match client-side (Telegram
    server search is always substring-based; we need exact).  User
    names/usernames come from ``result.users`` — no separate
    ``get_dialogs`` call.
    """

    def __init__(self, settings: Settings, telegram: TelegramService) -> None:
        self.settings = settings
        self.telegram = telegram

    # ------------------------------------------------------------------ search

    async def search(self, query: str) -> list[SearchResultItem]:
        """Search for an exact message match in private chats only.

        One ``SearchGlobalRequest(users_only=True)`` call → filter
        exact matches → enrich with ``result.users``.
        """
        query = query.strip()
        if not query:
            return []

        client = self.telegram.require_client()
        top_n = self.settings.search_top_matches
        started = time.time()

        # ── ONE API call: server-side global search, users only ──
        result = await self.telegram.safe_call(
            client,
            functions.messages.SearchGlobalRequest(
                q=query,
                filter=types.InputMessagesFilterEmpty(),
                min_date=None,
                max_date=None,
                offset_rate=0,
                offset_peer=types.InputPeerEmpty(),
                offset_id=0,
                limit=50,
                users_only=True,
            ),
        )

        # ── Build a user-lookup dict from the response ──
        user_map: dict[int, User] = {}
        for u in getattr(result, "users", []):
            if isinstance(u, User):
                user_map[u.id] = u

        # ── Filter for exact match, private chats only ──
        results: list[SearchResultItem] = []
        for m in result.messages:
            if len(results) >= top_n:
                break
            if not m or not getattr(m, "message", None):
                continue
            # Exact match — the message text is exactly the query.
            if m.message.strip() != query:
                continue
            # Only private (user) chats.
            peer_id = getattr(m, "peer_id", None)
            chat_id = getattr(peer_id, "user_id", None) if peer_id else None
            if chat_id is None:
                continue
            # Enrich with user info from the response itself.
            user = user_map.get(chat_id)
            results.append(
                SearchResultItem(
                    chat_id=chat_id,
                    username=user.username if user else None,
                    name=self._display_name(user) if user else f"user_{chat_id}",
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
