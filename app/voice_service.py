from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from telethon.tl.types import User

from .config import Settings
from .logger import get_logger
from .models import SendVoiceResponse, VoiceTemplate
from .telegram_client import TelegramService

logger = get_logger("voice_service")

TEMPLATE_FILES: dict[VoiceTemplate, str] = {
    "expired": "expired.ogg",
    "limited": "limited.ogg"
}


class VoiceServiceError(Exception):
    pass


class VoiceService:
    def __init__(self, settings: Settings, telegram: TelegramService) -> None:
        self.settings = settings
        self.telegram = telegram

    def resolve_path(self, template: VoiceTemplate) -> Path:
        if template not in TEMPLATE_FILES:
            raise VoiceServiceError(f"Unknown template: {template}")
        path = self.settings.voices_path / TEMPLATE_FILES[template]
        if not path.exists():
            raise VoiceServiceError(
                f"Voice file not found for template '{template}': {path}"
            )
        return path

    async def send(self, chat_id: int, template: VoiceTemplate) -> SendVoiceResponse:
        client = self.telegram.require_client()

        # Make sure the entity is a private user (defense-in-depth)
        try:
            entity = await self.telegram.safe_call(client.get_entity, chat_id)
        except Exception as e:  # noqa: BLE001
            logger.error("Cannot resolve chat %s: %s", chat_id, e)
            raise VoiceServiceError(f"Cannot resolve chat_id {chat_id}: {e}") from e

        if not isinstance(entity, User):
            raise VoiceServiceError(
                "Refusing to send: target is not a private user chat."
            )
        if entity.bot:
            raise VoiceServiceError("Refusing to send: target is a bot.")

        voice_path = self.resolve_path(template)

        try:
            message = await self.telegram.safe_call(
                client.send_file,
                entity,
                str(voice_path),
                voice_note=True,
                attributes=None,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("send_file failed for chat %s", chat_id)
            raise VoiceServiceError(f"Failed to send voice: {e}") from e

        sent_at = datetime.now(timezone.utc).isoformat()
        logger.info(
            "Sent voice '%s' to %s (msg_id=%s)",
            template,
            chat_id,
            getattr(message, "id", None),
        )

        return SendVoiceResponse(
            chat_id=chat_id,
            template=template,
            file=voice_path.name,
            message_id=getattr(message, "id", 0),
            sent_at=sent_at,
        )
