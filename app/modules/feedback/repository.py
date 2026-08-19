"""Data access for customer feedback - and nothing else.

Per the boundary rules this is the only code permitted to query the ``feedback`` table,
and it never touches another module's. The chat module reaches feedback through
``feedback.service``, never through here.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, session_scope
from app.modules.feedback.schemas import DEFAULT_SENTIMENT, Feedback

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CustomerFeedback(Base):
    """One opinion, recorded as the customer expressed it."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Their own words, kept verbatim - this is the whole point of the record.
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    sentiment: Mapped[str] = mapped_column(String(16), default=DEFAULT_SENTIMENT, index=True)
    customer_name: Mapped[str] = mapped_column(String(120), default="")
    contact: Mapped[str] = mapped_column(String(160), default="")
    # Set when the feedback is about a specific purchase rather than the shop generally.
    order_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True, default="")
    channel: Mapped[str] = mapped_column(String(32), default="web")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ReviewRequest(Base):
    """An order this conversation is owed feedback on, once it arrives.

    Written when the order is placed, read on every later turn of the same conversation.
    It holds the order number and nothing else about the order - the live state is read
    from Shopify each time, because "has it arrived yet" is not a fact we can cache.
    """

    __tablename__ = "feedback_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    order_number: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Set once the customer has answered - or declined - so they are asked only once.
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)


def expect_review(conversation_id: str, order_number: str) -> None:
    """Note that this conversation should be asked about this order once it arrives."""
    if not (conversation_id and order_number):
        return
    with session_scope() as session:
        already = session.execute(
            select(ReviewRequest.id)
            .where(ReviewRequest.conversation_id == conversation_id)
            .where(ReviewRequest.order_number == order_number)
        ).first()
        if already is not None:
            return
        session.add(ReviewRequest(
            conversation_id=conversation_id, order_number=order_number))


def open_review(conversation_id: str) -> Optional[str]:
    """The order number this conversation is still owed feedback on, if any."""
    if not conversation_id:
        return None
    with session_scope() as session:
        return session.execute(
            select(ReviewRequest.order_number)
            .where(ReviewRequest.conversation_id == conversation_id)
            .where(ReviewRequest.closed_at.is_(None))
            .order_by(ReviewRequest.id.desc())
        ).scalars().first()


def close_reviews(conversation_id: str) -> None:
    """Stop asking this conversation about its orders."""
    if not conversation_id:
        return
    with session_scope() as session:
        rows = session.execute(
            select(ReviewRequest)
            .where(ReviewRequest.conversation_id == conversation_id)
            .where(ReviewRequest.closed_at.is_(None))
        ).scalars().all()
        for row in rows:
            row.closed_at = _now()


def _to_feedback(row: CustomerFeedback) -> Feedback:
    return Feedback(
        comment=row.comment,
        sentiment=row.sentiment,
        customer_name=row.customer_name,
        contact=row.contact,
        order_number=row.order_number,
        conversation_id=row.conversation_id,
        channel=row.channel,
        created_at=row.created_at,
    )


def create(feedback: Feedback) -> Feedback:
    """Store one piece of feedback."""
    with session_scope() as session:
        row = CustomerFeedback(
            comment=feedback.comment,
            sentiment=feedback.sentiment,
            customer_name=feedback.customer_name,
            contact=feedback.contact,
            order_number=feedback.order_number,
            conversation_id=feedback.conversation_id,
            channel=feedback.channel,
        )
        session.add(row)
        session.flush()
        stored = _to_feedback(row)
    return stored


def recent_for_conversation(conversation_id: str, within_seconds: float) -> List[Feedback]:
    """Feedback recorded for this conversation in the last ``within_seconds``.

    Used to keep one opinion from being filed three times because the customer said
    thank you three times.
    """
    if not conversation_id or within_seconds <= 0:
        return []
    cutoff = _now() - timedelta(seconds=within_seconds)
    with session_scope() as session:
        rows = session.execute(
            select(CustomerFeedback)
            .where(CustomerFeedback.conversation_id == conversation_id)
            .where(CustomerFeedback.created_at >= cutoff)
            .order_by(CustomerFeedback.id.desc())
        ).scalars().all()
        return [_to_feedback(row) for row in rows]


def recent(limit: int = 50) -> List[Feedback]:
    """The newest feedback first, for whoever eventually reads it back."""
    with session_scope() as session:
        rows = session.execute(
            select(CustomerFeedback)
            .order_by(CustomerFeedback.id.desc())
            .limit(max(1, limit))
        ).scalars().all()
        return [_to_feedback(row) for row in rows]


def count() -> int:
    """Used by tests and diagnostics."""
    with session_scope() as session:
        return len(session.execute(select(CustomerFeedback.id)).all())
