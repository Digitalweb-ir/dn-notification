from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

VoiceTemplate = Literal["expired", "limited", "custom"]


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=512, description="Search keyword")


class SearchResultItem(BaseModel):
    chat_id: int
    username: Optional[str] = None
    name: str
    message: str
    message_date: str  # ISO 8601
    message_id: int
    match_score: float = Field(..., ge=0.0, le=1.0)


class SearchResponse(BaseModel):
    query: str
    count: int
    results: list[SearchResultItem]


class SendVoiceRequest(BaseModel):
    chat_id: int = Field(..., description="Telegram user/chat ID")
    template: VoiceTemplate = "custom"


class SendVoiceResponse(BaseModel):
    chat_id: int
    template: str
    file: str
    message_id: int
    sent_at: str


class SendMessageRequest(BaseModel):
    chat_id: int = Field(..., description="Telegram user/chat ID")
    shortcut: str = Field(..., min_length=1, max_length=64, description="Quick Reply shortcut name (without /)")


class SendMessageResponse(BaseModel):
    chat_id: int
    shortcut: str
    message_count: int = Field(..., description="Number of messages sent from the Quick Reply")
    sent_at: str


class HealthResponse(BaseModel):
    status: str
    telegram_connected: bool
    session: str
