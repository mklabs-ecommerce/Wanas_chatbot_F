"""Feedback module - the public surface for what customers think of the shop.

Deliberately not the same thing as a support ticket. A ticket is a problem awaiting an
action; feedback is an opinion awaiting nothing. Filing praise as a ticket would clog the
queue a person works through, and filing a complaint as feedback would bury it.

Two decisions from the store owner (2026-08-19) shape this module:

1. **No score is asked for.** Customers say what they think in their own words; there is
   no "rate us 1 to 5". The sentiment stored alongside the comment is read off what they
   wrote, never requested from them.
2. **Only unhappy feedback is emailed.** Everything is stored, but praise does not fill
   the owner's inbox. See ``schemas.DEFAULT_SENTIMENT`` for which way an unreadable
   sentiment falls, and why.

Like ``support``, the record is stored first and the notification is best-effort - a dead
mail server must never lose what a customer said.
"""

import logging
from datetime import datetime
from typing import List, Optional

from app.core.config import settings
from app.modules.feedback import repository
from app.modules.feedback.schemas import DEFAULT_SENTIMENT, SENTIMENTS, Feedback
from app.modules.notifications import service as notifications
from app.modules.orders import service as orders_service
from app.modules.orders.schemas import Order

logger = logging.getLogger(__name__)

MIN_COMMENT_LENGTH = 3
MAX_COMMENT_LENGTH = 2000


class FeedbackRejected(ValueError):
    """The feedback cannot be recorded as asked, and the customer needs to hear why."""


async def record_feedback(
    comment: str,
    sentiment: str = DEFAULT_SENTIMENT,
    customer_name: str = "",
    contact: str = "",
    order_number: Optional[str] = None,
    conversation_id: str = "",
    channel: str = "web",
) -> Feedback:
    """Write down what a customer said about the shop, and tell someone if it is bad.

    Stored before anyone is notified, so a mail failure downgrades the outcome from
    "the owner was emailed" to "it is written down" rather than losing it.
    """
    comment = (comment or "").strip()
    if len(comment) < MIN_COMMENT_LENGTH:
        # Recording an empty opinion would put a blank row in front of whoever reads
        # these back, and teach nobody anything.
        raise FeedbackRejected("There is nothing recorded yet - what did they say?")
    comment = comment[:MAX_COMMENT_LENGTH]

    sentiment = (sentiment or "").strip().lower()
    if sentiment not in SENTIMENTS:
        # Falls to negative on purpose (see schemas.DEFAULT_SENTIMENT): an unreadable
        # sentiment on a real complaint must reach a person, not be filed as fine.
        logger.info("Unknown feedback sentiment %r; treating as %s",
                    sentiment, DEFAULT_SENTIMENT)
        sentiment = DEFAULT_SENTIMENT

    existing = _recent_duplicate(conversation_id)
    if existing is not None:
        # A customer who says thank you twice has one opinion, not two.
        logger.info("Reusing feedback from conversation %s rather than recording a duplicate",
                    conversation_id or "unknown")
        return existing

    feedback = repository.create(Feedback(
        comment=comment,
        sentiment=sentiment,
        customer_name=(customer_name or "").strip()[:120],
        contact=(contact or "").strip()[:160],
        order_number=(order_number or None),
        conversation_id=conversation_id,
        channel=channel,
    ))
    logger.info("Recorded %s feedback from conversation %s",
                feedback.sentiment, conversation_id or "unknown")

    # They have answered, so stop asking - whatever they said, and whether or not it
    # was about the order we were waiting on.
    repository.close_reviews(conversation_id)

    if feedback.is_negative:
        # Best-effort by design: the return value is not checked, because there is
        # nothing useful to tell the customer either way.
        await notifications.notify_new_feedback(feedback)
    return feedback


def expect_review(conversation_id: str, order_number: str) -> None:
    """Remember that this conversation should be asked about this order once it lands.

    Called when the order is placed; nothing is asked at that moment. What the customer
    thinks of a garment they have not received yet is not information.
    """
    repository.expect_review(conversation_id, (order_number or "").strip())


async def review_due(conversation_id: str) -> Optional[Order]:
    """The order this conversation may now be asked about, or None.

    "May now" means the goods are demonstrably in the customer's hands - see
    ``Order.reached_the_customer``, which for cash on delivery means the courier has
    collected the money at the door.

    The order is re-read from Shopify on every call rather than cached, because the whole
    question is whether its state has changed since last time.

    Never raises. This runs on the way into an ordinary customer turn, and Shopify being
    slow or down must cost the feedback prompt, not the conversation.
    """
    order_number = repository.open_review(conversation_id)
    if not order_number:
        return None
    try:
        # The staff lookup, deliberately: this conversation created this order, so it is
        # entitled to it, and there is no customer contact to verify against here.
        order = await orders_service.lookup_for_staff(order_number)
    except Exception as exc:  # noqa: BLE001 - never break a chat over this
        logger.warning("Could not check order %s for review: %s", order_number, exc)
        return None

    if order is None or not order.reached_the_customer:
        return None
    return order


def close_review(conversation_id: str) -> None:
    """Stop asking about this conversation's orders - they answered, or declined."""
    repository.close_reviews(conversation_id)


def recent_feedback(limit: int = 50) -> List[Feedback]:
    """The newest feedback first. For a future owner-facing view, not for the model."""
    return repository.recent(limit)


def _recent_duplicate(conversation_id: str) -> Optional[Feedback]:
    """Feedback already recorded for this conversation moments ago."""
    for feedback in repository.recent_for_conversation(
            conversation_id, settings.feedback_duplicate_window_seconds):
        return feedback
    return None


def feedback_for_conversation(conversation_id: str) -> List[Feedback]:
    """What this conversation thought of the shop. For the owner-facing view."""
    return repository.all_for_conversation(conversation_id)


def feedback_in_range(start: datetime, end: datetime, channel: Optional[str] = None) -> List[Feedback]:
    """Every piece of feedback recorded in ``[start, end)``, optionally one channel."""
    return repository.in_range(start, end, channel)
