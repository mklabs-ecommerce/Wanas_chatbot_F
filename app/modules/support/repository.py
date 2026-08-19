"""Data access for support tickets - and nothing else.

Per the boundary rules this is the only code permitted to query the ``support_tickets``
table, and it never touches another module's. The chat module reaches tickets through
``support.service``, never through here.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, session_scope
from app.modules.support.schemas import STATUS_OPEN, Ticket

logger = logging.getLogger(__name__)

# Unambiguous when read aloud down a phone line: no O/0, I/1, or similar.
_REFERENCE_ALPHABET = "ACDEFGHJKLMNPQRTUVWXY34679"
_REFERENCE_LENGTH = 6
_REFERENCE_PREFIX = "WG-"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SupportTicket(Base):
    """One logged issue awaiting a human."""

    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # What the customer is given to quote later, e.g. "WG-K7P3QX".
    reference: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    customer_name: Mapped[str] = mapped_column(String(120), default="")
    # Email or phone, as the customer gave it - this is how the store replies.
    contact: Mapped[str] = mapped_column(String(160), default="")
    order_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Which conversation it came from, so the transcript can be found if needed.
    conversation_id: Mapped[str] = mapped_column(String(36), index=True, default="")
    channel: Mapped[str] = mapped_column(String(32), default="web")
    status: Mapped[str] = mapped_column(String(16), default=STATUS_OPEN, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


def _to_ticket(row: SupportTicket) -> Ticket:
    return Ticket(
        reference=row.reference,
        category=row.category,
        summary=row.summary,
        customer_name=row.customer_name,
        contact=row.contact,
        order_number=row.order_number,
        conversation_id=row.conversation_id,
        channel=row.channel,
        status=row.status,
        created_at=row.created_at,
    )


def new_reference() -> str:
    """A short reference a customer can read out without being misheard."""
    body = "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(_REFERENCE_LENGTH))
    return _REFERENCE_PREFIX + body


def create(ticket: Ticket) -> Ticket:
    """Store a ticket, assigning a reference if it has none."""
    with session_scope() as session:
        for _attempt in range(5):
            reference = ticket.reference or new_reference()
            if session.execute(
                select(SupportTicket.id).where(SupportTicket.reference == reference)
            ).first() is None:
                break
            # Astronomically unlikely, but a duplicate reference would send a customer
            # to somebody else's complaint.
            logger.warning("Ticket reference %s already exists; drawing another", reference)
            ticket.reference = ""
        else:
            raise RuntimeError("Could not allocate a unique ticket reference")

        row = SupportTicket(
            reference=reference,
            category=ticket.category,
            summary=ticket.summary,
            customer_name=ticket.customer_name,
            contact=ticket.contact,
            order_number=ticket.order_number,
            conversation_id=ticket.conversation_id,
            channel=ticket.channel,
            status=ticket.status or STATUS_OPEN,
        )
        session.add(row)
        session.flush()
        stored = _to_ticket(row)
    return stored


def get(reference: str) -> Optional[Ticket]:
    with session_scope() as session:
        row = session.execute(
            select(SupportTicket).where(SupportTicket.reference == (reference or "").strip())
        ).scalar_one_or_none()
        return _to_ticket(row) if row is not None else None


def recent_for_conversation(conversation_id: str, within_seconds: float) -> List[Ticket]:
    """Tickets logged for this conversation in the last ``within_seconds``.

    Used to keep one complaint from becoming five because the customer restated it.
    """
    if not conversation_id or within_seconds <= 0:
        return []
    cutoff = _now() - timedelta(seconds=within_seconds)
    with session_scope() as session:
        rows = session.execute(
            select(SupportTicket)
            .where(SupportTicket.conversation_id == conversation_id)
            .where(SupportTicket.created_at >= cutoff)
            .order_by(SupportTicket.id.desc())
        ).scalars().all()
        return [_to_ticket(row) for row in rows]


def count() -> int:
    """Used by tests and diagnostics."""
    with session_scope() as session:
        return len(session.execute(select(SupportTicket.id)).all())


def all_for_conversation(conversation_id: str) -> List[Ticket]:
    """Every ticket from this conversation, newest first, however old.

    Separate from ``recent_for_conversation`` on purpose: that one answers "is this a
    duplicate", which is a question about the last half hour. This one answers "what
    did this conversation produce", which has no time limit.
    """
    if not conversation_id:
        return []
    with session_scope() as session:
        rows = session.execute(
            select(SupportTicket)
            .where(SupportTicket.conversation_id == conversation_id)
            .order_by(SupportTicket.id.desc())
        ).scalars().all()
        return [_to_ticket(row) for row in rows]
