"""The engagement module's own tables. Nothing outside this module touches them.

Three of them, each answering one question:

``instagram_events``   - have we already handled this comment or message?
``instagram_posts``    - which product is this post showing?
``instagram_threads``  - which conversation belongs to this Instagram user?

The first is the load-bearing one. Meta redelivers any event it did not get a prompt
200 for, so without a claim a redelivery means a second public reply, a second like, or
a private reply spent on a comment that already had its one and only DM.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import DateTime, Integer, String, Text, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, session_scope
from app.modules.engagement.schemas import PostProduct

logger = logging.getLogger(__name__)

KIND_COMMENT = "comment"
KIND_MESSAGE = "message"

# Outcomes worth telling apart when reading the log back.
OUTCOME_PENDING = "pending"
OUTCOME_DONE = "done"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InstagramEvent(Base):
    """One comment or message we have taken responsibility for.

    The row is written *before* the work starts, which is the whole point: the claim is
    what makes a redelivered webhook a no-op instead of a second public reply.
    """

    __tablename__ = "instagram_events"

    # Meta's own id - a comment id or a message mid.
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # What the comment was judged to be; null for direct messages.
    classification: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # What was actually done about it, in words, so a dry run is readable.
    action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), default=OUTCOME_PENDING, nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)


class InstagramPost(Base):
    """Which catalog product a post or reel is showing.

    Cached against the post, not the comment: resolving costs a Shopify read and
    sometimes a model request, and a post collects many comments.
    """

    __tablename__ = "instagram_posts"

    media_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Empty when the post could not be matched. Stored anyway - see PostProduct.
    title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    color: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class InstagramThread(Base):
    """One Instagram user's ongoing conversation.

    The mapping is what makes a returning customer's history survive: the same person
    messaging next week lands back in the same ``conversations`` row, with the orders
    and the feedback promises already attached to it.
    """

    __tablename__ = "instagram_threads"

    # Meta's scoped id for this person on this account.
    igsid: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    # The comment that started it, when one did.
    opened_from_comment: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_inbound_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    messages_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# --- events ---------------------------------------------------------------


def claim(event_id: str, kind: str) -> bool:
    """Take responsibility for an event. ``False`` means someone already has.

    The primary key does the work: a concurrent or redelivered claim loses the insert
    and is told so, rather than both callers believing they are first.
    """
    event_id = (event_id or "").strip()
    if not event_id:
        return False
    try:
        with session_scope() as session:
            session.add(InstagramEvent(event_id=event_id, kind=kind))
        return True
    except IntegrityError:
        logger.info("Instagram %s %s was already claimed; ignoring the redelivery",
                    kind, event_id)
        return False


def finish(event_id: str, outcome: str, classification: Optional[str] = None,
           action: str = "") -> None:
    """Record how an event ended. Best-effort bookkeeping, never the work itself."""
    with session_scope() as session:
        row = session.get(InstagramEvent, event_id)
        if row is None:
            return
        row.outcome = outcome
        row.finished_at = _now()
        if classification is not None:
            row.classification = classification
        if action:
            row.action = action[:2000]


def release(event_id: str) -> None:
    """Give a claim back so a later delivery may try again.

    Only for failures that happened *before* anything was sent - a refused download, a
    model that never answered. Never after an outward call, successful or ambiguous:
    a comment gets one private reply for all time, and a retry would spend it.
    """
    with session_scope() as session:
        row = session.get(InstagramEvent, event_id)
        if row is not None:
            session.delete(row)


def handled(event_id: str) -> Optional[dict]:
    """What was decided about an event, or None. For the dashboard and for tests."""
    with session_scope() as session:
        row = session.get(InstagramEvent, event_id)
        if row is None:
            return None
        return {
            "event_id": row.event_id,
            "kind": row.kind,
            "classification": row.classification,
            "action": row.action,
            "outcome": row.outcome,
        }


def recent_events(limit: int = 50, kind: Optional[str] = None) -> List[dict]:
    """The newest handled events first, for the owner-facing view."""
    with session_scope() as session:
        statement = select(InstagramEvent).order_by(InstagramEvent.claimed_at.desc())
        if kind:
            statement = statement.where(InstagramEvent.kind == kind)
        rows = session.execute(statement.limit(max(1, limit))).scalars().all()
        return [{
            "event_id": row.event_id,
            "kind": row.kind,
            "classification": row.classification,
            "action": row.action,
            "outcome": row.outcome,
            "claimed_at": row.claimed_at,
        } for row in rows]


def comment_count_in_range(start: datetime, end: datetime) -> int:
    """How many comments were claimed (received) in ``[start, end)``. For the funnel KPI."""
    with session_scope() as session:
        query = (
            select(func.count())
            .select_from(InstagramEvent)
            .where(InstagramEvent.kind == KIND_COMMENT)
            .where(InstagramEvent.claimed_at >= start)
            .where(InstagramEvent.claimed_at < end)
        )
        return int(session.execute(query).scalar_one())


def opened_thread_count_in_range(start: datetime, end: datetime) -> int:
    """How many DM threads were opened *from a comment* in ``[start, end)``.

    Only threads with ``opened_from_comment`` set - a thread a customer opened by
    messaging directly, with no comment behind it, is not part of this funnel.
    """
    with session_scope() as session:
        query = (
            select(func.count())
            .select_from(InstagramThread)
            .where(InstagramThread.opened_from_comment.is_not(None))
            .where(InstagramThread.created_at >= start)
            .where(InstagramThread.created_at < end)
        )
        return int(session.execute(query).scalar_one())


# --- posts ----------------------------------------------------------------


def post_product(media_id: str, fresh_for_seconds: float = 0.0) -> Optional[PostProduct]:
    """The product cached against a post, or None if it has never been resolved.

    ``fresh_for_seconds`` above zero re-resolves anything older, which is only useful
    for an unmatched post: a post's subject does not change, but the catalog behind it
    does, and a piece added after the post went up could match it later.
    """
    if not media_id:
        return None
    with session_scope() as session:
        row = session.get(InstagramPost, media_id)
        if row is None:
            return None
        if fresh_for_seconds > 0 and not row.title:
            cutoff = _now() - timedelta(seconds=fresh_for_seconds)
            resolved = row.resolved_at
            if resolved is not None and resolved.tzinfo is None:
                resolved = resolved.replace(tzinfo=timezone.utc)
            if resolved is None or resolved < cutoff:
                return None
        return PostProduct(media_id=row.media_id, title=row.title, color=row.color,
                           source=row.source, resolved_at=row.resolved_at)


def save_post_product(product: PostProduct) -> None:
    """Remember what a post is showing - including that it could not be worked out."""
    with session_scope() as session:
        row = session.get(InstagramPost, product.media_id)
        if row is None:
            row = InstagramPost(media_id=product.media_id)
            session.add(row)
        row.title = (product.title or "")[:255]
        row.color = (product.color or "")[:120]
        row.source = (product.source or "")[:16]
        row.resolved_at = _now()


# --- threads --------------------------------------------------------------


def thread(igsid: str) -> Optional[dict]:
    """This person's conversation, or None if they have never written before."""
    if not igsid:
        return None
    with session_scope() as session:
        row = session.get(InstagramThread, igsid)
        if row is None:
            return None
        return {
            "igsid": row.igsid,
            "conversation_id": row.conversation_id,
            "username": row.username,
            "opened_from_comment": row.opened_from_comment,
            "messages_in": row.messages_in,
        }


def thread_for_conversation(conversation_id: str) -> Optional[dict]:
    """The Instagram thread behind a conversation, or None if it isn't one.

    ``conversation_id`` is indexed on this table, so this costs one lookup - used to
    find the igsid to send an owner's reply to.
    """
    if not conversation_id:
        return None
    with session_scope() as session:
        row = session.execute(
            select(InstagramThread).where(InstagramThread.conversation_id == conversation_id)
        ).scalars().first()
        if row is None:
            return None
        return {
            "igsid": row.igsid,
            "conversation_id": row.conversation_id,
            "username": row.username,
        }


def link_thread(igsid: str, conversation_id: str, username: str = "",
                opened_from_comment: Optional[str] = None) -> None:
    """Attach an Instagram user to a conversation, keeping the first one they got."""
    with session_scope() as session:
        row = session.get(InstagramThread, igsid)
        if row is None:
            session.add(InstagramThread(
                igsid=igsid,
                conversation_id=conversation_id,
                username=(username or "")[:120],
                opened_from_comment=opened_from_comment,
            ))
            return
        # The conversation is never repointed: their history lives on the id they have.
        if username and not row.username:
            row.username = username[:120]
        if opened_from_comment and not row.opened_from_comment:
            row.opened_from_comment = opened_from_comment


def record_inbound(igsid: str) -> None:
    """Note that they wrote to us, for the owner-facing view."""
    with session_scope() as session:
        row = session.get(InstagramThread, igsid)
        if row is not None:
            row.last_inbound_at = _now()
            row.messages_in = (row.messages_in or 0) + 1


def thread_count() -> int:
    """Used by tests and diagnostics."""
    with session_scope() as session:
        return len(session.execute(select(InstagramThread.igsid)).scalars().all())
