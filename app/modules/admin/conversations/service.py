"""Assembling the conversation list and detail view from what other modules expose.

This module mostly reads and composes: every fact comes from another module's public
``service.py`` - orders are read live from Shopify so what the owner sees is the order
as it stands now, everything else from the local database. Gathered defensively
throughout: Shopify being unreachable should cost the orders column of one
conversation, never the whole page.

One write path exists: ``reply()``/``resume()`` let the owner answer an Instagram DM by
hand and hand it back to the bot - see ``app/modules/admin/conversations/__init__.py``
for what is and is not in scope. Both go through ``engagement.service`` and
``chat.service``, never around them.
"""

import logging
from typing import Any, Dict, List, Optional

from app.modules.chat import service as chat_service
from app.modules.engagement import service as engagement_service
from app.modules.feedback import service as feedback_service
from app.modules.orders import service as orders_service
from app.modules.support import service as support_service

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
# How the owner may order the list. Newest first is the default because the question
# behind opening this page is usually "what just happened".
SORTS = ("recent", "oldest", "messages", "tickets", "pieces", "orders", "feedback")
DEFAULT_SORT = "recent"
# Enough to see how a conversation went without loading a whole day into a browser.
TRANSCRIPT_LIMIT = 200


def _when(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def list_conversations(channel: Optional[str], limit: int = DEFAULT_LIMIT,
                       sort: str = DEFAULT_SORT) -> Dict[str, Any]:
    """Every recent conversation on one channel, or every channel if ``channel`` is
    ``None`` (the "All" tab), with a count of what each one produced.

    Deliberately cheap: counts come from the local database only. ``limit`` is applied
    before sorting, so it always means "the last N conversations on this channel", and
    re-sorting them never silently changes which ones are being looked at.
    """
    rows: List[Dict[str, Any]] = []
    for summary in chat_service.conversations(limit, channel):
        conversation_id = summary["conversation_id"]
        row_channel = summary["channel"]
        tickets = support_service.tickets_for_conversation(conversation_id)
        feedback = feedback_service.feedback_for_conversation(conversation_id)
        rows.append({
            "conversation_id": conversation_id,
            "channel": row_channel,
            "customer_name": _customer_name(conversation_id, row_channel, tickets, feedback),
            "started_at": _when(summary["started_at"]),
            "last_at": _when(summary["last_at"]),
            "message_count": summary["message_count"],
            "last_message": summary["last_message"],
            "order_count": len(orders_service.order_numbers_for_conversation(conversation_id)),
            "ticket_count": len(tickets),
            "feedback_count": len(feedback),
            "piece_count": orders_service.pieces_ordered_in_conversation(conversation_id),
            # A checkout link that was never paid - a sale that nearly happened.
            "unpaid_link_count": orders_service.unpaid_links_in_conversation(conversation_id),
            # So a row with an unhappy customer can be spotted without opening it.
            "worst_sentiment": _worst(feedback),
        })

    sort = sort if sort in SORTS else DEFAULT_SORT
    rows.sort(key=_sort_key(sort), reverse=(sort != "oldest"))
    return {"channel": channel or "all", "count": len(rows), "sort": sort, "conversations": rows}


def _sort_key(sort: str):
    """How to order the list. Ties fall back to time, so the order is never arbitrary."""
    def newest(row):
        return row["last_at"] or ""

    if sort in ("recent", "oldest"):
        return newest
    field = {"messages": "message_count", "tickets": "ticket_count",
             "pieces": "piece_count", "orders": "order_count",
             "feedback": "feedback_count"}[sort]
    return lambda row: (row[field], newest(row))


async def get_conversation(conversation_id: str) -> Optional[Dict[str, Any]]:
    """One conversation and everything that came out of it, or ``None`` if it does not
    exist. Read-only: no reply, no takeover - see ``app/modules/admin/__init__.py``.

    The orders are read live from Shopify rather than from anything stored here, so
    what the owner sees is the order as it stands now - including a status that changed
    long after the chat ended.
    """
    conversation = chat_service.get_conversation(conversation_id)
    if conversation is None:
        return None

    transcript = _safely(lambda: chat_service.transcript(conversation_id, TRANSCRIPT_LIMIT),
                         "transcript", conversation_id, default=[])
    feedback = _safely(lambda: feedback_service.feedback_for_conversation(conversation_id),
                       "feedback", conversation_id, default=[])
    tickets = _safely(lambda: support_service.tickets_for_conversation(conversation_id),
                      "tickets", conversation_id, default=[])

    try:
        orders = await orders_service.orders_for_conversation(conversation_id)
        orders_readable = True
    except Exception as exc:  # noqa: BLE001 - Shopify must not cost the whole page
        logger.warning("Could not read orders for conversation %s: %s", conversation_id, exc)
        orders, orders_readable = [], False

    return {
        "conversation_id": conversation_id,
        "channel": conversation["channel"],
        "customer_name": _customer_name(conversation_id, conversation["channel"], tickets, feedback),
        "owner_active": conversation["owner_active"],
        "taken_over_at": _when(conversation["taken_over_at"]),
        "messages": [_message_row(message) for message in transcript],
        "orders": [_order_row(order) for order in orders],
        # Told apart from "no orders", which is a different fact the owner may act on.
        "orders_readable": orders_readable,
        "feedback": [{
            "comment": item.comment,
            "sentiment": item.sentiment,
            "label": item.label,
            "order_number": item.order_number,
            "customer_name": item.customer_name,
            "contact": item.contact,
            "created_at": _when(item.created_at),
        } for item in feedback],
        "tickets": [{
            "reference": ticket.reference,
            "category": ticket.category,
            "label": ticket.label,
            "summary": ticket.summary,
            "status": ticket.status,
            "order_number": ticket.order_number,
            "contact": ticket.contact,
            "created_at": _when(ticket.created_at),
        } for ticket in tickets],
    }


def _order_row(order) -> Dict[str, Any]:
    """An order as the owner needs it - fuller than the customer-facing shape.

    ``Order.to_tool_dict()`` deliberately strips the address and contact, because it is
    built for the model. This reader is the store, looking at its own orders.
    """
    return {
        "number": order.number,
        "placed_on": order.placed_on,
        "status": order.status_words,
        "payment_status": order.financial_status.lower().replace("_", " "),
        "cash_on_delivery": order.cash_on_delivery,
        "arrived": order.reached_the_customer,
        "total": (order.total + " " + order.currency).strip(),
        "subtotal": order.subtotal,
        "delivery": order.delivery,
        "currency": order.currency,
        "phone": order.phone,
        "email": order.email,
        "ships_to": ", ".join(part for part in (order.ships_to_city,
                                                order.ships_to_country) if part),
        "cancelled": order.is_cancelled,
        "admin_url": orders_service.admin_url(order),
        "items": [{
            "title": item.title,
            "quantity": item.quantity,
            "variant": item.variant_title,
        } for item in order.items],
    }


def _customer_name(conversation_id: str, channel: str, tickets, feedback) -> str:
    """Best available name for this conversation's title - never blank.

    Instagram carries a real handle; everything else only has a name if the customer
    gave one while filing a ticket or leaving feedback. Resolved once here rather than
    in the frontend, so both the list and the detail panel agree and neither has to
    duplicate the fallback.
    """
    if channel == "instagram":
        username = engagement_service.username_for_conversation(conversation_id)
        if username:
            return "@" + username
    for source in (tickets, feedback):
        for item in source:
            if item.customer_name:
                return item.customer_name
    return "عميل " + conversation_id[:8]


def _message_row(message: Dict[str, Any]) -> Dict[str, Any]:
    """A transcript row plus who actually wrote it.

    ``role`` alone can't tell a bot reply from an owner's own reply - both are stored as
    ROLE_MODEL so the model's own history stays a clean back-and-forth (see
    chat/repository.py's OWNER_PROVIDER). ``provider`` is what disambiguates it here.
    """
    if message["role"] == "user":
        author = "customer"
    elif message.get("provider") == "owner":
        author = "owner"
    else:
        author = "bot"
    return {"role": message["role"], "author": author, "content": message["content"]}


def _worst(feedback) -> Optional[str]:
    """The sentiment worth noticing in a list, if there is one."""
    for wanted in ("negative", "neutral", "positive"):
        if any(item.sentiment == wanted for item in feedback):
            return wanted
    return None


def _safely(read, what: str, conversation_id: str, default):
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 - one broken column, not a broken page
        logger.warning("Could not read %s for conversation %s: %s", what, conversation_id, exc)
        return default


async def reply(conversation_id: str, text: str) -> Dict[str, Any]:
    """Send the owner's own reply into an Instagram conversation, and pause the bot.

    Instagram only, for now - the web widget has no push channel to deliver a reply
    through outside its own request/response cycle. ``engagement.service`` owns the
    actual send and only stores the message once it has confirmed Instagram received
    it, so this never reports success for a message the customer never got.
    """
    conversation = chat_service.get_conversation(conversation_id)
    if conversation is None or conversation["channel"] != "instagram":
        return {"error": "not_found"}
    text = text.strip()
    if not text:
        return {"error": "empty"}
    sent = await engagement_service.send_owner_reply(conversation_id, text)
    return {"sent": True} if sent else {"error": "delivery_failed"}


def resume(conversation_id: str) -> Dict[str, Any]:
    """Hand an Instagram conversation back to the bot."""
    conversation = chat_service.get_conversation(conversation_id)
    if conversation is None or conversation["channel"] != "instagram":
        return {"error": "not_found"}
    chat_service.resume_bot(conversation_id)
    return {"resumed": True}
