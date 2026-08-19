"""Provider-neutral shapes exchanged between the LLM clients and the chat module.

Kept deliberately small and free of any provider SDK import, so ``integrations/llm.py``
can move between models - and, if a second provider is ever added, between providers -
without the chat module noticing.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

Role = Literal["user", "model"]


@dataclass
class ImagePart:
    """An image attached to a user turn (used from step 5 onwards)."""

    data: bytes
    mime_type: str


@dataclass
class AudioPart:
    """A recording attached to a user turn - a voice note (step 12 onwards)."""

    data: bytes
    mime_type: str


@dataclass
class Turn:
    """One message in a conversation, as handed to a provider."""

    role: Role
    text: str = ""
    images: List[ImagePart] = field(default_factory=list)
    audio: List[AudioPart] = field(default_factory=list)
    # Populated when replaying a previous assistant tool call / its result (step 3+).
    tool_calls: List["ToolCall"] = field(default_factory=list)
    tool_results: List["ToolResult"] = field(default_factory=list)


@dataclass
class ToolCall:
    """A model's request to run one of our functions."""

    name: str
    arguments: Dict[str, Any]
    id: Optional[str] = None
    # Gemini 3.x signs each function-call part and rejects the follow-up request if the
    # signature is not replayed verbatim. Opaque to us; other providers ignore it.
    signature: Optional[bytes] = None


@dataclass
class ToolResult:
    """What our function returned, to be fed back to the model."""

    name: str
    result: Dict[str, Any]
    id: Optional[str] = None


@dataclass
class LLMResponse:
    """A single provider reply, normalised."""

    text: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    # True when this came from a fallback rather than the configured primary model.
    degraded: bool = False


class LLMError(Exception):
    """Base class for provider failures."""


class LLMRateLimited(LLMError):
    """Provider refused the request for quota reasons (HTTP 429)."""


class LLMUnavailable(LLMError):
    """Provider is temporarily unable to serve the request (HTTP 5xx, timeouts, empty body)."""


class LLMAllProvidersFailed(LLMError):
    """Every configured model and provider was exhausted."""
