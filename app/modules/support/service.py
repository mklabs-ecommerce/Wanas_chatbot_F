"""Support module - the public surface for issues that need a human.

The bot can answer questions and place orders; it cannot refund, replace, re-route a
parcel or apologise on the brand's behalf. When it hits one of those, the honest move is
to write the problem down where a person will see it, and tell the customer it has been
done.

Two things this module owns:

1. **The tickets table**, through its own ``repository``. Nothing else queries it.
2. **Telling someone**, by calling ``notifications.service`` - which is a separate module
   precisely so that a mail server being down cannot lose a customer's complaint. The
   ticket is stored first and the notification is best-effort, never the other way round.
"""

import logging
from datetime import datetime
from typing import List, Optional

from app.core.config import settings
from app.modules.notifications import service as notifications
from app.modules.support import repository
from app.modules.support.schemas import CATEGORIES, STATUS_OPEN, Ticket

logger = logging.getLogger(__name__)

MIN_SUMMARY_LENGTH = 10
MAX_SUMMARY_LENGTH = 2000


class TicketRejected(ValueError):
    """The ticket cannot be logged as asked, and the customer needs to hear why."""


async def create_ticket(
    category: str,
    summary: str,
    customer_name: str = "",
    contact: str = "",
    order_number: Optional[str] = None,
    conversation_id: str = "",
    channel: str = "web",
) -> Ticket:
    """Log an issue for a human, and try to tell them about it.

    The ticket is stored before anyone is notified, so a mail failure downgrades the
    outcome from "someone was emailed" to "it is written down" rather than losing it.
    """
    category = (category or "").strip().lower()
    if category not in CATEGORIES:
        # An unknown category would quietly break the store owner's mail filters, and
        # "other" is always a truthful answer.
        logger.info("Unknown ticket category %r; filing as other", category)
        category = "other"

    summary = (summary or "").strip()
    if len(summary) < MIN_SUMMARY_LENGTH:
        raise TicketRejected("The issue needs to be described before it can be logged.")
    summary = summary[:MAX_SUMMARY_LENGTH]

    contact = (contact or "").strip()
    if not contact:
        # Without this the store can read the complaint and do nothing about it.
        raise TicketRejected("An email address or phone number is needed to reply.")

    existing = _recent_duplicate(conversation_id, category)
    if existing is not None:
        # A customer restating the same problem, or the model calling twice. Either way
        # the store owner should not get the same complaint five times.
        logger.info("Reusing ticket %s rather than logging a duplicate", existing.reference)
        return existing

    ticket = repository.create(Ticket(
        reference="",
        category=category,
        summary=summary,
        customer_name=(customer_name or "").strip()[:120],
        contact=contact[:160],
        order_number=(order_number or None),
        conversation_id=conversation_id,
        channel=channel,
        status=STATUS_OPEN,
    ))
    logger.info("Logged ticket %s (%s) from conversation %s",
                ticket.reference, ticket.category, conversation_id or "unknown")

    # Best-effort by design: the return value is not checked, because there is nothing
    # useful to tell the customer either way - their issue is recorded regardless.
    await notifications.notify_new_ticket(ticket)
    return ticket


def get_ticket(reference: str) -> Optional[Ticket]:
    """Look a ticket up by the reference the customer was given."""
    return repository.get((reference or "").strip().upper())


def _recent_duplicate(conversation_id: str, category: str) -> Optional[Ticket]:
    """An open ticket of the same kind, from this conversation, logged moments ago."""
    for ticket in repository.recent_for_conversation(
            conversation_id, settings.ticket_duplicate_window_seconds):
        if ticket.category == category and ticket.status == STATUS_OPEN:
            return ticket
    return None


def tickets_for_conversation(conversation_id: str) -> List[Ticket]:
    """Issues logged from this conversation. For the owner-facing view.

    Unbounded in time, unlike the duplicate check - the owner wants the whole history
    of a conversation, not just what was logged in the last half hour.
    """
    return repository.all_for_conversation(conversation_id)


def tickets_in_range(start: datetime, end: datetime, channel: Optional[str] = None) -> List[Ticket]:
    """Every ticket created in ``[start, end)``, optionally one channel. For admin.analytics."""
    return repository.in_range(start, end, channel)
