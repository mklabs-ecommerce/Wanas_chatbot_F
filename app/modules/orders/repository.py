"""The one thing about orders this app stores locally: which conversation placed one.

Orders themselves live in Shopify and are always read from there - nothing about an
order's contents, status or totals is cached here, because all of it can change without
this app being told. What Shopify cannot answer is "which chat produced this order",
so that link, and only that link, is kept here.

The same goes for a payment link handed out for online payment: Shopify knows the draft,
but not which chat produced it, nor whether the customer has been told it went through.

Per the boundary rules this is the only code permitted to query ``conversation_orders``
and ``conversation_drafts``. Other modules ask ``orders.service`` for both.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, session_scope

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConversationOrder(Base):
    """An order placed during a particular conversation."""

    __tablename__ = "conversation_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    # The customer-facing number, e.g. "#1008" - what every other lookup here uses.
    order_number: Mapped[str] = mapped_column(String(32), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="web")
    # How many garments were on the order. Stored rather than read back from Shopify so
    # the owner's list view can sort by it without one API call per row.
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    # Set once the customer has been told this order is on its way, so they are told
    # once and not on every message afterwards. Lives here rather than in a notifications
    # table because it is a fact about this order in this conversation.
    shipped_told_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


def link(conversation_id: str, order_number: str, channel: str = "web",
         item_count: int = 0) -> None:
    """Record that this conversation placed this order. Idempotent."""
    if not (conversation_id and order_number):
        return
    with session_scope() as session:
        already = session.execute(
            select(ConversationOrder.id)
            .where(ConversationOrder.conversation_id == conversation_id)
            .where(ConversationOrder.order_number == order_number)
        ).first()
        if already is not None:
            return
        session.add(ConversationOrder(conversation_id=conversation_id,
                                      order_number=order_number, channel=channel,
                                      item_count=max(0, int(item_count or 0))))


def order_numbers_for(conversation_id: str) -> List[str]:
    """Order numbers placed in this conversation, newest first."""
    if not conversation_id:
        return []
    with session_scope() as session:
        return list(session.execute(
            select(ConversationOrder.order_number)
            .where(ConversationOrder.conversation_id == conversation_id)
            .order_by(ConversationOrder.id.desc())
        ).scalars().all())


def conversation_for(order_number: str) -> Optional[str]:
    """Which conversation placed this order, if this app placed it at all."""
    if not order_number:
        return None
    with session_scope() as session:
        return session.execute(
            select(ConversationOrder.conversation_id)
            .where(ConversationOrder.order_number == order_number)
            .order_by(ConversationOrder.id.desc())
        ).scalars().first()


def count() -> int:
    """Used by tests and diagnostics."""
    with session_scope() as session:
        return len(session.execute(select(ConversationOrder.id)).all())


def piece_count_for(conversation_id: str) -> int:
    """How many garments this conversation ordered, across all its orders."""
    if not conversation_id:
        return 0
    with session_scope() as session:
        counts = session.execute(
            select(ConversationOrder.item_count)
            .where(ConversationOrder.conversation_id == conversation_id)
        ).scalars().all()
        return sum(count or 0 for count in counts)


def orders_not_yet_told_shipped(conversation_id: str) -> List[str]:
    """Order numbers from this conversation the customer has not been told about."""
    if not conversation_id:
        return []
    with session_scope() as session:
        return list(session.execute(
            select(ConversationOrder.order_number)
            .where(ConversationOrder.conversation_id == conversation_id)
            .where(ConversationOrder.shipped_told_at.is_(None))
            .order_by(ConversationOrder.id.desc())
        ).scalars().all())


def mark_shipped_told(conversation_id: str, order_number: str) -> None:
    """Remember that this customer has been told this order is on its way."""
    if not (conversation_id and order_number):
        return
    with session_scope() as session:
        rows = session.execute(
            select(ConversationOrder)
            .where(ConversationOrder.conversation_id == conversation_id)
            .where(ConversationOrder.order_number == order_number)
            .where(ConversationOrder.shipped_told_at.is_(None))
        ).scalars().all()
        for row in rows:
            row.shipped_told_at = _now()


class ConversationDraft(Base):
    """A payment link handed out during a conversation, and what became of it.

    Kept apart from ``ConversationOrder`` on purpose: a draft is not an order. It becomes
    one only when the customer pays, and that is the moment ``order_number`` is filled in
    and the row is also linked as a real order.
    """

    __tablename__ = "conversation_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    # Shopify's global id, e.g. "gid://shopify/DraftOrder/1120…" - what the poll reads.
    draft_id: Mapped[str] = mapped_column(String(96), index=True)
    # The draft's own name, e.g. "#D1". Never shown to a customer: it is not their order
    # number, and quoting it would give them something staff cannot look up as an order.
    draft_name: Mapped[str] = mapped_column(String(32), default="")
    channel: Mapped[str] = mapped_column(String(32), default="web")
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    # The real order, once they have paid. Null while the link is still unpaid.
    order_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Its Shopify id as well, because the order search cannot see an order this new -
    # measured 2026-08-19: a lookup by number half a second after payment finds nothing.
    order_id: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)
    # Set once we know how this ended - paid, or gone from Shopify. Stops the poll.
    settled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # Set once the customer has been told the payment came through, so they hear it once.
    paid_told_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


def link_draft(conversation_id: str, draft_id: str, draft_name: str = "",
               channel: str = "web", item_count: int = 0) -> None:
    """Record that this conversation handed out this payment link. Idempotent."""
    if not (conversation_id and draft_id):
        return
    with session_scope() as session:
        already = session.execute(
            select(ConversationDraft.id)
            .where(ConversationDraft.conversation_id == conversation_id)
            .where(ConversationDraft.draft_id == draft_id)
        ).first()
        if already is not None:
            return
        session.add(ConversationDraft(conversation_id=conversation_id, draft_id=draft_id,
                                      draft_name=draft_name, channel=channel,
                                      item_count=max(0, int(item_count or 0))))


def unsettled_drafts(conversation_id: str) -> List[Dict[str, Any]]:
    """Payment links from this conversation whose fate is still unknown."""
    if not conversation_id:
        return []
    with session_scope() as session:
        rows = session.execute(
            select(ConversationDraft)
            .where(ConversationDraft.conversation_id == conversation_id)
            .where(ConversationDraft.settled_at.is_(None))
            .order_by(ConversationDraft.id.desc())
        ).scalars().all()
        return [{"draft_id": row.draft_id, "draft_name": row.draft_name,
                 "channel": row.channel, "item_count": row.item_count} for row in rows]


def settle_draft(conversation_id: str, draft_id: str,
                 order_number: Optional[str] = None,
                 order_id: Optional[str] = None) -> None:
    """Record how a payment link ended: paid (with its order), or simply gone."""
    if not (conversation_id and draft_id):
        return
    with session_scope() as session:
        rows = session.execute(
            select(ConversationDraft)
            .where(ConversationDraft.conversation_id == conversation_id)
            .where(ConversationDraft.draft_id == draft_id)
        ).scalars().all()
        for row in rows:
            row.settled_at = _now()
            if order_number:
                row.order_number = order_number
            if order_id:
                row.order_id = order_id


def drafts_awaiting_payment_news(conversation_id: str) -> List[Dict[str, Any]]:
    """Orders paid for through a link here that the customer has not been told about."""
    if not conversation_id:
        return []
    with session_scope() as session:
        rows = session.execute(
            select(ConversationDraft)
            .where(ConversationDraft.conversation_id == conversation_id)
            .where(ConversationDraft.order_number.is_not(None))
            .where(ConversationDraft.paid_told_at.is_(None))
            .order_by(ConversationDraft.id.desc())
        ).scalars().all()
        return [{"order_number": row.order_number, "order_id": row.order_id}
                for row in rows]


def mark_payment_told(conversation_id: str, order_number: str) -> None:
    """Remember this customer has been told their payment came through."""
    if not (conversation_id and order_number):
        return
    with session_scope() as session:
        rows = session.execute(
            select(ConversationDraft)
            .where(ConversationDraft.conversation_id == conversation_id)
            .where(ConversationDraft.order_number == order_number)
            .where(ConversationDraft.paid_told_at.is_(None))
        ).scalars().all()
        for row in rows:
            row.paid_told_at = _now()


def draft_count() -> int:
    """Used by tests and diagnostics."""
    with session_scope() as session:
        return len(session.execute(select(ConversationDraft.id)).all())
