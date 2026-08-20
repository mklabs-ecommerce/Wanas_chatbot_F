"""Data shapes for Instagram engagement.

The webhook payloads are Meta's, deeply nested and full of fields nothing here needs.
These dataclasses are the boundary: everything past this file works with a
``CommentEvent`` or a ``MessageEvent`` and never with raw JSON.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

# What a comment can be. Ordered by how much the shop does about it.
IMPORTANT = "important"
POSITIVE = "positive"
NEGATIVE = "negative"
NEITHER = "neither"
CLASSES = (IMPORTANT, POSITIVE, NEGATIVE, NEITHER)

# Where an unreadable classification falls. "neither" means no public action at all,
# which is the only safe direction: a wrong like is recoverable, a wrong public reply
# is read by everyone who sees the post, and a wrong DM to a stranger is worse still.
DEFAULT_CLASS = NEITHER


@dataclass
class CommentEvent:
    """Someone commented on one of our posts."""

    comment_id: str
    media_id: str
    text: str = ""
    author_id: str = ""
    username: str = ""
    # Set when this comment is itself a reply to another comment.
    parent_id: Optional[str] = None
    created_at: Optional[datetime] = None

    @property
    def handle(self) -> str:
        """How the owner would find this person, e.g. ``@wanas.customer``."""
        return ("@" + self.username) if self.username else (self.author_id or "unknown")


@dataclass
class Attachment:
    """A photo or voice note on a direct message, as Meta hands it over: a URL."""

    kind: str  # "image" | "audio" | "video" | "share" | "story" | other
    url: str = ""


@dataclass
class MessageEvent:
    """Someone sent us a direct message."""

    message_id: str
    sender_id: str
    text: str = ""
    attachments: List[Attachment] = field(default_factory=list)
    # True when Meta is echoing a message *we* sent. These arrive on the same webhook as
    # real ones, and answering them is how a bot ends up talking to itself.
    is_echo: bool = False
    created_at: Optional[datetime] = None


@dataclass
class Classification:
    """What a comment turned out to be, and how that was decided.

    ``source`` is kept because the cheap paths and the model disagree in interesting
    ways, and because a run in dry mode is only readable if it says why.
    """

    kind: str = DEFAULT_CLASS
    reason: str = ""
    source: str = "model"  # "model" | "rule" | "default"

    @property
    def acts_publicly(self) -> bool:
        return self.kind in (IMPORTANT, POSITIVE)


@dataclass
class PostProduct:
    """Which catalog product a post is showing, once resolved.

    ``title`` is empty when the post could not be matched. That is stored too, and on
    purpose: an unmatched post must not be re-resolved on every comment it collects, and
    the bot asking "which piece did you mean?" is a better answer than a guess.
    """

    media_id: str
    title: str = ""
    color: str = ""
    source: str = ""  # "caption" | "image" | "" when unresolved
    resolved_at: Optional[datetime] = None

    @property
    def resolved(self) -> bool:
        return bool(self.title)

    def describe(self) -> str:
        """The piece as a person would say it, for the DM opener."""
        if not self.resolved:
            return ""
        return self.title + ((" - " + self.color) if self.color else "")


def parse_webhook(payload: Dict[str, Any]) -> Sequence[Any]:
    """Pull the events we care about out of one webhook delivery.

    Meta batches: one POST can carry several entries, each with several changes or
    messages. Anything unrecognised is dropped silently - new webhook fields appear
    without warning, and an unknown one is not an error.
    """
    events: List[Any] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            event = _to_comment(change)
            if event is not None:
                events.append(event)
        for item in entry.get("messaging") or []:
            event = _to_message(item)
            if event is not None:
                events.append(event)
    return events


def _to_comment(change: Dict[str, Any]) -> Optional[CommentEvent]:
    if not isinstance(change, dict) or change.get("field") != "comments":
        return None
    value = change.get("value")
    if not isinstance(value, dict):
        return None
    comment_id = str(value.get("id") or "")
    if not comment_id:
        return None

    author = value.get("from") or {}
    media = value.get("media") or {}
    return CommentEvent(
        comment_id=comment_id,
        media_id=str(media.get("id") or ""),
        text=str(value.get("text") or ""),
        author_id=str(author.get("id") or ""),
        username=str(author.get("username") or ""),
        parent_id=str(value.get("parent_id")) if value.get("parent_id") else None,
        created_at=_when(value.get("timestamp")),
    )


def _to_message(item: Dict[str, Any]) -> Optional[MessageEvent]:
    if not isinstance(item, dict):
        return None
    message = item.get("message")
    if not isinstance(message, dict):
        # Read receipts, reactions and delivery confirmations share this webhook.
        return None
    message_id = str(message.get("mid") or "")
    if not message_id:
        return None

    attachments = []
    for raw in message.get("attachments") or []:
        if not isinstance(raw, dict):
            continue
        url = ((raw.get("payload") or {}).get("url") or "")
        attachments.append(Attachment(kind=str(raw.get("type") or ""), url=str(url)))

    return MessageEvent(
        message_id=message_id,
        sender_id=str((item.get("sender") or {}).get("id") or ""),
        text=str(message.get("text") or ""),
        attachments=attachments,
        is_echo=bool(message.get("is_echo")),
        created_at=_when(item.get("timestamp")),
    )


def _when(value: Any) -> Optional[datetime]:
    """Meta sends milliseconds since the epoch here, and ISO-8601 elsewhere."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1_000_000_000_000 else value
        try:
            return datetime.utcfromtimestamp(seconds)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
