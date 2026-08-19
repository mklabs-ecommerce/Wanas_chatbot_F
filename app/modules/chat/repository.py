"""Data access for conversation history - and nothing else.

Per the boundary rules, this is the only code permitted to query the ``conversations``
and ``chat_messages`` tables, and it must never touch another module's tables. Orders,
tickets and feedback are reached through their own modules' service functions.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, session_scope

logger = logging.getLogger(__name__)

# Roles as stored. "model" matches the LLM-side vocabulary so no translation is needed.
ROLE_USER = "user"
ROLE_MODEL = "model"


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
            select(ChatMessage.role, ChatMessage.content)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.id.desc())
            .limit(limit)
        )
        # Read the columns inside the session: committing expires ORM instances, so
        # touching their attributes afterwards raises DetachedInstanceError.
        rows = [{"role": role, "content": content}
                for role, content in session.execute(stmt).all()]
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
