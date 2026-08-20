"""The chat module's public surface for the rest of the app.

Everything else in this module is internal: ``agent.py`` runs the turn, ``repository.py``
owns the conversation tables, ``tools.py`` reaches outwards. This file is the one door in.

It answers two things. What was actually said in a conversation, because a support
ticket is read by a person who then has to work out what happened - the ticket's summary
is the *model's* account of a chat it also conducted, so the transcript is the evidence
behind it, and the two disagreeing is itself worth seeing.

And, since Instagram, how to hold a turn at all. ``handle_message()`` is the door every
channel comes through: the web widget's route calls the agent directly because it lives
inside this module, and everything outside calls this instead. That is what keeps one
assistant answering every channel rather than a second one growing in the adapter.
"""

import logging
from datetime import datetime
from typing import Iterable, List, Optional, Sequence, Tuple

from app.modules.chat import agent, attachments, repository
from app.modules.chat.attachments import AttachmentError
from app.modules.chat.schemas import Answer

logger = logging.getLogger(__name__)

# Enough to see how an issue arose without turning an email into a novel.
DEFAULT_TRANSCRIPT_LIMIT = 20


def transcript(
    conversation_id: Optional[str],
    limit: int = DEFAULT_TRANSCRIPT_LIMIT,
) -> List[dict]:
    """Return what was said in a conversation, oldest first.

    Rows are ``{"role": "user"|"model", "content": str}``. An unknown conversation is an
    empty list rather than an error: a caller wanting to attach context must not fail
    because there is none.
    """
    if not conversation_id:
        return []
    return repository.get_recent_messages(conversation_id, max(1, limit))


def conversations(limit: int = 50) -> List[dict]:
    """Every recent conversation, newest first, for the owner-facing view.

    Summaries only - the messages themselves come from ``transcript()``.
    """
    return repository.recent_conversations(max(1, limit))


def conversation_count_in_range(start: datetime, end: datetime,
                                channel: Optional[str] = None) -> int:
    """How many conversations started in ``[start, end)``, optionally one channel."""
    return repository.conversation_count_in_range(start, end, channel)


def customer_message_count_in_range(start: datetime, end: datetime,
                                    channel: Optional[str] = None) -> int:
    """How many customer messages arrived in ``[start, end)``, optionally one channel.

    Customer messages only (``role="user"``) - a conversation volume KPI should not be
    inflated by counting the bot's own replies too.
    """
    return repository.message_count_in_range(start, end, channel, role=repository.ROLE_USER)


async def handle_message(
    text: str = "",
    raw_attachments: Sequence[Tuple[bytes, Optional[str]]] = (),
    conversation_id: Optional[str] = None,
    channel: str = "web",
) -> Answer:
    """Hold one turn of a conversation on any channel.

    ``raw_attachments`` is ``(bytes, declared_mime)`` pairs - what a channel that hands
    over files rather than base64 has. Which are photos and which are recordings is
    decided by sniffing the bytes, never by the label: the rule that a declared content
    type is not trusted is older than this function, and Instagram is no more reliable
    about it than a browser is.

    Raises ``AttachmentError`` when an attachment cannot be used. Those messages are
    written to be read by a customer, so an adapter can send one on as the reply.
    """
    images, audio = _sort_attachments(raw_attachments)

    reply = await agent.handle_message(
        message=(text or "").strip(),
        images=images,
        audio=audio,
        conversation_id=conversation_id,
        channel=channel,
    )
    return Answer(
        conversation_id=reply.conversation_id,
        text=reply.text,
        model=reply.model,
        provider=reply.provider,
        degraded=reply.degraded,
        tools_used=list(reply.tools_used),
        transcript=reply.transcript,
    )


def _sort_attachments(raw: Iterable[Tuple[bytes, Optional[str]]]):
    """Split downloaded files into photos and recordings, then validate each as usual."""
    image_uploads = []
    audio_uploads = []
    for data, declared in raw or ():
        if not data:
            continue
        if attachments.sniff_mime_type(data) is not None:
            image_uploads.append(attachments.from_bytes(data, declared))
        elif attachments.sniff_audio_type(data) is not None:
            audio_uploads.append(attachments.from_bytes(data, declared))
        else:
            # A video, a sticker, a shared post - nothing the assistant can read. Saying
            # so is better than dropping it silently and answering as if nothing came.
            logger.info("Ignoring an attachment of an unusable type (%s, %d bytes)",
                        declared or "unknown", len(data))
            raise AttachmentError(
                "I can read photos and voice notes, but not that kind of file. "
                "Could you send a photo or tell me in a message?"
            )

    return (attachments.decode_images(image_uploads),
            attachments.decode_audio(audio_uploads))


def seed_conversation(
    customer_text: str,
    assistant_text: str,
    channel: str = "web",
    conversation_id: Optional[str] = None,
) -> str:
    """Start a conversation that has already had its first exchange elsewhere.

    Instagram is the reason this exists. A comment is answered with a fixed opener sent
    straight to Meta, so by the time the assistant is involved two things have already
    been said - and the assistant needs both, or it greets someone who has already been
    greeted and asks what they want when they have already said.

    Both messages are stored as what they are. Nothing here calls a model.
    """
    conversation_id = repository.ensure_conversation(conversation_id, channel=channel)
    customer_text = (customer_text or "").strip()
    assistant_text = (assistant_text or "").strip()
    if customer_text:
        repository.add_message(conversation_id, repository.ROLE_USER, customer_text)
    if assistant_text:
        repository.add_message(conversation_id, repository.ROLE_MODEL, assistant_text)
    return conversation_id
