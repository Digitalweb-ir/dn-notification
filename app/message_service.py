from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

from telethon.tl import functions, types
from telethon.tl.types import User

from .config import Settings
from .logger import get_logger
from .models import SendMessageResponse
from .telegram_client import TelegramService

logger = get_logger("message_service")


class MessageServiceError(Exception):
    pass


class MessageService:
    """Sends messages to a private chat via two modes.

    **Quick Reply mode** (``shortcut``):
        Telegram Business accounts can define Quick Replies (shortcuts like
        ``/message``) in the app.  This service looks up a shortcut by name,
        resolves its message IDs, and sends them to a target user using
        ``messages.SendQuickReplyMessagesRequest``.

    **Direct text mode** (``message``):
        Sends a raw text message directly using ``client.send_message()``.
        Newlines (``\\n``) in the string are preserved as-is.
    """

    def __init__(self, settings: Settings, telegram: TelegramService) -> None:
        self.settings = settings
        self.telegram = telegram

    # ── shared helpers ────────────────────────────────────────────────

    async def _resolve_user(self, chat_id: int) -> Tuple[User, object]:
        """Resolve *chat_id* to a real user and return (entity, input_peer).

        Raises ``MessageServiceError`` if the target is not a private user
        (e.g. a bot, channel, or group).
        """
        client = self.telegram.require_client()

        try:
            entity = await self.telegram.safe_call(client.get_entity, chat_id)
        except Exception as e:  # noqa: BLE001
            logger.error("Cannot resolve chat %s: %s", chat_id, e)
            raise MessageServiceError(
                f"Cannot resolve chat_id {chat_id}: {e}"
            ) from e

        if not isinstance(entity, User):
            raise MessageServiceError(
                "Refusing to send: target is not a private user chat."
            )
        if entity.bot:
            raise MessageServiceError("Refusing to send: target is a bot.")

        input_peer = await client.get_input_entity(chat_id)
        return entity, input_peer

    # ── public API ─────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: int,
        shortcut: Optional[str] = None,
        message: Optional[str] = None,
    ) -> SendMessageResponse:
        if shortcut:
            return await self._send_quick_reply(chat_id, shortcut)
        return await self._send_text(chat_id, message)  # type: ignore[arg-type]

    # ── Quick Reply path ───────────────────────────────────────────────

    async def _send_quick_reply(
        self, chat_id: int, shortcut: str
    ) -> SendMessageResponse:
        client = self.telegram.require_client()
        entity, input_peer = await self._resolve_user(chat_id)

        # ── 1. Fetch all Quick Replies and find the matching shortcut ──
        try:
            result = await self.telegram.safe_call(
                client,
                functions.messages.GetQuickRepliesRequest(hash=0),
            )
        except Exception as e:  # noqa: BLE001
            raise MessageServiceError(
                f"Failed to fetch Quick Replies: {e}"
            ) from e

        if isinstance(result, types.messages.QuickRepliesNotModified):
            raise MessageServiceError(
                "No Quick Replies found on this account."
            )

        matching: Optional[types.QuickReply] = None
        for qr in result.quick_replies:
            if qr.shortcut == shortcut:
                matching = qr
                break

        if matching is None:
            available = [qr.shortcut for qr in result.quick_replies]
            raise MessageServiceError(
                f"Quick Reply shortcut '{shortcut}' not found. "
                f"Available: {available or '(none)'}"
            )

        # ── 2. Get the message IDs inside that shortcut ──
        try:
            msgs_result = await self.telegram.safe_call(
                client,
                functions.messages.GetQuickReplyMessagesRequest(
                    shortcut_id=matching.shortcut_id,
                    hash=0,
                ),
            )
        except Exception as e:  # noqa: BLE001
            raise MessageServiceError(
                f"Failed to fetch messages for shortcut '{shortcut}': {e}"
            ) from e

        msg_ids = [m.id for m in msgs_result.messages]
        if not msg_ids:
            raise MessageServiceError(
                f"Quick Reply '{shortcut}' has no messages."
            )

        # ── 3. Send the quick reply messages to the target chat ──
        try:
            await self.telegram.safe_call(
                client,
                functions.messages.SendQuickReplyMessagesRequest(
                    peer=input_peer,
                    shortcut_id=matching.shortcut_id,
                    id=msg_ids,
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("SendQuickReplyMessages failed for chat %s", chat_id)
            raise MessageServiceError(
                f"Failed to send quick reply '{shortcut}': {e}"
            ) from e

        sent_at = datetime.now(timezone.utc).isoformat()
        logger.info(
            "Sent quick reply '%s' (%d message(s)) to chat %s",
            shortcut,
            len(msg_ids),
            chat_id,
        )

        return SendMessageResponse(
            chat_id=chat_id,
            shortcut=shortcut,
            message_count=len(msg_ids),
            sent_at=sent_at,
        )

    # ── Direct text path ───────────────────────────────────────────────

    async def _send_text(
        self, chat_id: int, message: str
    ) -> SendMessageResponse:
        client = self.telegram.require_client()
        entity, _ = await self._resolve_user(chat_id)

        try:
            sent = await self.telegram.safe_call(
                client.send_message,
                entity,
                message,
                parse_mode=None,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("send_message failed for chat %s", chat_id)
            raise MessageServiceError(
                f"Failed to send message: {e}"
            ) from e

        sent_at = datetime.now(timezone.utc).isoformat()
        msg_id = getattr(sent, "id", 0)
        logger.info(
            "Sent text message to chat %s (msg_id=%s, len=%d)",
            chat_id,
            msg_id,
            len(message),
        )

        return SendMessageResponse(
            chat_id=chat_id,
            message_id=msg_id,
            sent_at=sent_at,
        )
