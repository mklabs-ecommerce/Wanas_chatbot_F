"""Data access for conversation history - and nothing else.

Per the boundary rules, this is the only code permitted to query the ``conversations``
and ``chat_messages`` tables, and it must never touch another module's tables. Orders,
tickets and feedback are reached through their own modules' service functions.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, session_scope

logger = logging.getLogger(__name__)

# Roles as stored. "model" matches the LLM-side vocabulary so no translation is needed.
ROLE_USER = "user"
ROLE_MODEL = "model"

# An owner's own reply is stored with role=ROLE_MODEL (not a role of its own) so it
# still reads as an assistant turn to Gemini once the bot resumes - a new role would
# make the history builder in agent.py map it to "user", which would put two customer
# turns in a row and corrupt the conversation for the model. This ``provider`` value is
# how the dashboard tells an owner's own words apart from the bot's.
OWNER_PROVIDER = "owner"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Conversation(Base):
    """One ongoing chat with one customer."""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Which front-end the conversation came from. Only "web" exists today; WhatsApp and
    # friends would be added as adapters without touching this module.
    channel: Mapped[str] = mapped_column(String(32), default="web", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    # True while the owner is handling this conversation by hand from the dashboard -
    # the agent checks this and stops auto-replying until it is cleared. Instagram only
    # today; see chat/agent.py.
    owner_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    taken_over_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    messages: Mapped[List["ChatMessage"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ChatMessage.id",
    )


class ChatMessage(Base):
    """One message in a conversation."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Which model/provider produced a "model" message; null for customer messages.
    model: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


# --- operations ----------------------------------------------------------


def ensure_conversation(conversation_id: Optional[str], channel: str = "web") -> str:
    """Return an existing conversation id, or create a fresh conversation."""
    with session_scope() as session:
        if conversation_id:
            existing = session.get(Conversation, conversation_id)
            if existing is not None:
                return existing.id
            # An unknown id (e.g. the local database was reset while a browser tab kept
            # its id) is recreated rather than rejected, so the widget keeps working.
            logger.info("Recreating unknown conversation %s", conversation_id)
            new_id = conversation_id
        else:
            new_id = str(uuid.uuid4())

        session.add(Conversation(id=new_id, channel=channel))
        return new_id


def add_message(
    conversation_id: str,
    role: str,
    content: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> None:
    """Append one message to a conversation."""
    with session_scope() as session:
        session.add(
            ChatMessage(
                conversation_id=conversation_id,
                role=role,
                content=content,
                model=model,
                provider=provider,
            )
        )
        conversation = session.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.updated_at = _now()


def get_recent_messages(conversation_id: str, limit: int) -> List[dict]:
    """Return the last ``limit`` messages, oldest first, as plain dicts.

    Plain dicts rather than ORM objects so callers never hold a detached instance and
    cannot accidentally write through this module's models.
    """
    with session_scope() as session:
        stmt = (
            select(ChatMessage.role, ChatMessage.content, ChatMessage.provider)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.id.desc())
            .limit(limit)
        )
        # Read the columns inside the session: committing expires ORM instances, so
        # touching their attributes afterwards raises DetachedInstanceError.
        rows = [{"role": role, "content": content, "provider": provider}
                for role, content, provider in session.execute(stmt).all()]
    rows.reverse()
    return rows


def count_messages(conversation_id: str) -> int:
    """Number of messages stored for a conversation (used by tests and diagnostics)."""
    with session_scope() as session:
        stmt = (
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
        )
        return int(session.execute(stmt).scalar_one())


def conversation_count_in_range(start: datetime, end: datetime, channel: Optional[str] = None) -> int:
    """How many conversations started in ``[start, end)``, optionally one channel."""
    with session_scope() as session:
        query = (
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.created_at >= start)
            .where(Conversation.created_at < end)
        )
        if channel:
            query = query.where(Conversation.channel == channel)
        return int(session.execute(query).scalar_one())


def message_count_in_range(start: datetime, end: datetime, channel: Optional[str] = None,
                           role: Optional[str] = None) -> int:
    """How many messages were sent in ``[start, end)``, optionally one channel/role.

    Joins to ``conversations`` for the channel filter rather than storing channel on
    every message - a message never changes channel, so this costs nothing but a join.
    """
    with session_scope() as session:
        query = (
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.created_at >= start)
            .where(ChatMessage.created_at < end)
        )
        if role:
            query = query.where(ChatMessage.role == role)
        if channel:
            query = query.join(Conversation, Conversation.id == ChatMessage.conversation_id)
            query = query.where(Conversation.channel == channel)
        return int(session.execute(query).scalar_one())


def inbound_timestamps_in_range(start: datetime, end: datetime,
                                channel: Optional[str] = None) -> List[datetime]:
    """When each customer message arrived in ``[start, end)``, optionally one channel.

    Customer messages only - for the "busiest hours/days" KPI, which asks when
    customers actually reach out, not when the (near-instant) bot replies.
    """
    with session_scope() as session:
        query = (
            select(ChatMessage.created_at)
            .where(ChatMessage.created_at >= start)
            .where(ChatMessage.created_at < end)
            .where(ChatMessage.role == ROLE_USER)
        )
        if channel:
            query = query.join(Conversation, Conversation.id == ChatMessage.conversation_id)
            query = query.where(Conversation.channel == channel)
        return list(session.execute(query).scalars().all())


def recent_conversations(limit: int = 50, channel: Optional[str] = None) -> List[dict]:
    """Newest conversations first, with a count and the last thing said.

    For the owner-facing view. Returns plain dicts rather than ORM rows so nothing
    outside this module holds a live session object.
    """
    with session_scope() as session:
        query = select(Conversation).order_by(Conversation.id.desc())
        if channel:
            query = query.where(Conversation.channel == channel)
        rows = session.execute(query.limit(max(1, limit))).scalars().all()

        out = []
        for row in rows:
            messages = session.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == row.id)
                .order_by(ChatMessage.id.desc())
            ).scalars().all()
            last = messages[0] if messages else None
            out.append({
                "conversation_id": row.id,
                "channel": row.channel,
                "started_at": row.created_at,
                "last_at": last.created_at if last else row.created_at,
                "message_count": len(messages),
                "last_message": (last.content if last else ""),
            })
        return out


def get_conversation(conversation_id: str) -> Optional[dict]:
    """The bare conversation row - id, channel, timestamps - or None if it does not
    exist. For a caller that needs to tell "no such conversation" apart from "a
    conversation that genuinely has no messages yet"."""
    if not conversation_id:
        return None
    with session_scope() as session:
        row = session.get(Conversation, conversation_id)
        if row is None:
            return None
        return {"conversation_id": row.id, "channel": row.channel,
                "started_at": row.created_at, "last_at": row.updated_at,
                "owner_active": row.owner_active, "taken_over_at": row.taken_over_at}


def set_takeover(conversation_id: str, active: bool) -> None:
    """Mark whether the owner is handling this conversation by hand right now."""
    with session_scope() as session:
        row = session.get(Conversation, conversation_id)
        if row is None:
            return
        row.owner_active = active
        row.taken_over_at = _now() if active else None


def is_takeover_active(conversation_id: str) -> bool:
    with session_scope() as session:
        row = session.get(Conversation, conversation_id)
        return bool(row and row.owner_active)
