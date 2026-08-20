"""Request/response shapes for the chat endpoint, and for the module's public door."""

from dataclasses import dataclass, field as dataclass_field
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


class VoiceUpload(BaseModel):
    """A voice note attached to a customer turn.

    Same shape as ``ImageUpload`` and for the same reasons: base64 in the JSON body, so
    every channel hands over bytes the same way, and ``mime_type`` is advisory only.
    """

    data: str = Field(..., min_length=1)
    mime_type: Optional[str] = Field(default=None, max_length=64)


class ChatRequest(BaseModel):
    """One customer turn: text, images, a voice note, or a combination."""

    # May be blank when an image carries the whole message - a customer often sends a
    # photo with nothing typed. The route rejects a turn that has neither.
    message: str = Field(default="", max_length=4000)
    images: List[ImageUpload] = Field(default_factory=list)
    # A spoken message. Transcribed before anything else happens, and the transcript is
    # what the assistant, the history and every tool actually see.
    audio: List[VoiceUpload] = Field(default_factory=list)
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
    # What a voice note was heard as. Returned so the widget can show the customer their
    # own words back - a mishearing is obvious to them and to nobody else.
    transcript: Optional[str] = None


@dataclass
class Answer:
    """What ``chat.service.handle_message()`` gives back to another module.

    Deliberately not the agent's own reply object: ``agent.py`` is internal, and a
    caller holding its type would be reaching past this module's door. This carries only
    what a channel adapter needs to send a message and log what happened.
    """

    conversation_id: str
    text: str
    model: str = ""
    provider: str = ""
    degraded: bool = False
    tools_used: List[str] = dataclass_field(default_factory=list)
    # What a voice note was heard as, when one was sent.
    transcript: Optional[str] = None
