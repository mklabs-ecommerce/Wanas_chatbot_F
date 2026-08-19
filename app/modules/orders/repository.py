"""The one thing about orders this app stores locally: which conversation placed one.

Orders themselves live in Shopify and are always read from there - nothing about an
order's contents, status or totals is cached here, because all of it can change without
this app being told. What Shopify cannot answer is "which chat produced this order",
so that link, and only that link, is kept here.

Per the boundary rules this is the only code permitted to query ``conversation_orders``.
Other modules ask ``orders.service`` for it.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

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
