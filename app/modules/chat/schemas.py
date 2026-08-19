"""Request/response shapes for the chat endpoint."""

from typing import List, Optional

from pydantic import BaseModel, Field


class ImageUpload(BaseModel):
    """One image attached to a customer turn.

    ``data`` is base64, either bare or as a browser data URL
    ("data:image/jpeg;base64,..."), which is what FileReader hands the widget.
    ``mime_type`` is optional and advisory only - the bytes are sniffed either way.
    """

    data: str = Field(..., min_length=1)
    mime_type: Optional[str] = Field(default=None, max_length=64)


class ChatRequest(BaseModel):
    """One customer turn: text, images, or both."""

    # May be blank when an image carries the whole message - a customer often sends a
    # photo with nothing typed. The route rejects a turn that has neither.
    message: str = Field(default="", max_length=4000)
    images: List[ImageUpload] = Field(default_factory=list)
    # Omit on the first turn; the response carries the id to send back next time.
    conversation_id: Optional[str] = Field(default=None, max_length=36)
    # Which front-end this came from. The web widget is the only one today.
    channel: str = Field(default="web", max_length=32)


class ChatResponse(BaseModel):
    """The bot's reply, plus enough metadata to see what served it while testing."""

    conversation_id: str
    reply: str
    model: str
    provider: str
    # True when a fallback model or provider answered instead of the primary.
    degraded: bool = False
    # Which tools ran for this turn, so testing shows whether real data was used.
    tools_used: List[str] = Field(default_factory=list)
