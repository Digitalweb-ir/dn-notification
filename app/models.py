from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


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


class SendMessageRequest(BaseModel):
    chat_id: int = Field(..., description="Telegram user/chat ID")
    shortcut: Optional[str] = Field(
        None,
        min_length=1,
        max_length=64,
        description="Quick Reply shortcut name (without /)",
    )
    message: Optional[str] = Field(
        None,
        min_length=1,
        max_length=4096,
        description="Raw text message to send directly",
    )

    @model_validator(mode="after")
    def _check_mode(self) -> "SendMessageRequest":
        if bool(self.shortcut) == bool(self.message):
            raise ValueError(
                "Provide either 'shortcut' or 'message', not both and not neither"
            )
        return self


class SendMessageResponse(BaseModel):
    chat_id: int
    shortcut: Optional[str] = None
    message_count: Optional[int] = Field(
        None, description="Number of messages sent (Quick Reply mode)"
    )
    message_id: Optional[int] = Field(
        None, description="ID of the sent message (direct text mode)"
    )
    sent_at: str


class HealthResponse(BaseModel):
    status: str
    telegram_connected: bool
    session: str
