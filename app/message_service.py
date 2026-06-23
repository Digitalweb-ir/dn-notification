from __future__ import annotations

from datetime import datetime, timezone

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
    """Sends pre-defined Quick Reply messages to a private chat.

    Telegram Business accounts can define Quick Replies (shortcuts like
    ``/message``) in the app.  This service looks up a shortcut by name,
    resolves its message IDs, and sends them to a target user using
    ``messages.SendQuickReplyMessagesRequest`` — the server-side
    equivalent of tapping the quick reply in the Telegram app.
    """

    def __init__(self, settings: Settings, telegram: TelegramService) -> None:
        self.settings = settings
        self.telegram = telegram

    async def send(self, chat_id: int, shortcut: str) -> SendMessageResponse:
        client = self.telegram.require_client()

        # ── 1. Resolve target entity (must be a real user) ──
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

        # ── 2. Fetch all Quick Replies and find the matching shortcut ──
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

        matching: types.QuickReply | None = None
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

        # ── 3. Get the message IDs inside that shortcut ──
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

        # ── 4. Send the quick reply messages to the target chat ──
        try:
            input_peer = await client.get_input_entity(chat_id)
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
