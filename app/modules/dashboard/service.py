"""Assembling what the store owner needs to see, from what the modules already expose.

This module reads and composes; it decides nothing. Every fact here comes from another
module's public ``service.py`` - there is no SQL and no Shopify call in this file, and
there must never be one. If something is missing from a view, the fix is to add it to
the owning module's service, not to reach around it.

Everything is gathered defensively. A dashboard is a convenience: Shopify being
unreachable should cost the orders column, never the page.
"""

import logging
from typing import Any, Dict, List, Optional

from app.modules.chat import service as chat_service
from app.modules.feedback import service as feedback_service
from app.modules.orders import service as orders_service
from app.modules.support import service as support_service

logger = logging.getLogger(__name__)

DEFAULT_CONVERSATION_LIMIT = 50
# Enough to see how a conversation went without loading a whole day into a browser.
TRANSCRIPT_LIMIT = 200


def _when(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def overview(limit: int = DEFAULT_CONVERSATION_LIMIT) -> Dict[str, Any]:
    """Every recent conversation, with a count of what each one produced.

    Deliberately cheap: counts come from the local database only. Nothing here calls
    Shopify, so the list stays fast however many conversations there are - the live
    order details are fetched only when one conversation is opened.
    """
    rows: List[Dict[str, Any]] = []
    for summary in chat_service.conversations(limit):
        conversation_id = summary["conversation_id"]
        feedback = feedback_service.feedback_for_conversation(conversation_id)
        rows.append({
            "conversation_id": conversation_id,
            "channel": summary["channel"],
            "started_at": _when(summary["started_at"]),
            "last_at": _when(summary["last_at"]),
            "message_count": summary["message_count"],
            "last_message": summary["last_message"],
            "order_count": len(orders_service.order_numbers_for_conversation(conversation_id)),
            "ticket_count": len(support_service.tickets_for_conversation(conversation_id)),
            "feedback_count": len(feedback),
            # So a row with an unhappy customer can be spotted without opening it.
            "worst_sentiment": _worst(feedback),
        })
    return {"count": len(rows), "conversations": rows}


async def conversation(conversation_id: str) -> Dict[str, Any]:
    """One conversation and everything that came out of it.

    The orders are read live from Shopify rather than from anything stored here, so
    what the owner sees is the order as it stands now - including a status that changed
    long after the chat ended.
    """
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
        "messages": transcript,
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
