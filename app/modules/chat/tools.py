"""What the model is allowed to call, and which module service actually does it.

Every function here is deliberately thin: map arguments, call one module's ``service``,
shape the result as JSON. No Shopify calls, no SQL, no business rules - if logic starts
accumulating in this file it belongs in the owning module instead.

Declarations are plain JSON Schema, which Gemini accepts unchanged.
"""

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from dataclasses import dataclass, field

from app.core.config import settings
from app.integrations.llm_types import ImagePart
from app.modules.catalog import service as catalog_service
from app.modules.catalog.service import CatalogUnavailable
from app.modules.feedback import service as feedback_service
from app.modules.feedback.service import FeedbackRejected
from app.modules.orders import service as orders_service
from app.modules.notifications import service as notifications_service
from app.modules.orders.service import (
    CancelRefused,
    OrderRejected,
    OrdersUnavailable,
    RequestedItem,
)
from app.modules.support import service as support_service
from app.modules.support.schemas import CATEGORIES
from app.modules.support.service import TicketRejected

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """What a tool may know about the turn beyond the model's own arguments.

    Anything here is a fact about the conversation, not a choice the model gets to make -
    which is the point. The channel ends up as a tag on a real order, so it has to come
    from the request rather than from the model's guess about where it is talking.
    """

    images: Sequence[ImagePart] = field(default_factory=tuple)
    channel: str = "web"
    conversation_id: str = ""


MAX_SEARCH_RESULTS = 5
MAX_BROWSE_RESULTS = 8
MAX_ORDER_RESULTS = 5


SEARCH_PRODUCTS = {
    "name": "search_products",
    "description": (
        "Search the store's live product catalog by keyword. Use this for any question "
        "about what the store sells, prices, sizes, colours or availability - never "
        "answer such a question from memory. Pass English keywords describing the "
        "garment (for example 'hoodie', 'black t-shirt', 'oversized tee'); if the "
        "customer wrote in Arabic, translate their words to English keywords first. "
        "Returns the closest matching products with real prices and the sizes and "
        "colours that are actually in stock. An empty result means the store has "
        "nothing matching - say so honestly instead of suggesting something else."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "English keywords describing what the customer wants.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum products to return (1-5). Defaults to 5.",
            },
        },
        "required": ["query"],
    },
}


async def _search_products(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch to ``catalog.service.search_products``."""
    query = str(arguments.get("query") or "").strip()
    if not query:
        return {"error": "A search query is required."}

    limit = arguments.get("limit")
    try:
        limit = int(limit) if limit is not None else MAX_SEARCH_RESULTS
    except (TypeError, ValueError):
        limit = MAX_SEARCH_RESULTS
    limit = max(1, min(limit, MAX_SEARCH_RESULTS))

    try:
        products = await catalog_service.search_products(query, limit=limit)
    except CatalogUnavailable as exc:
        # Surfaced to the model as data, so it can apologise rather than invent stock.
        logger.error("search_products failed: %s", exc)
        # Data only, never instructions: anything phrased as a directive here gets
        # read out to the customer. How to behave is set in the system prompt.
        return {"error": "catalog_unavailable"}

    if not products:
        return {"query": query, "count": 0, "products": []}

    return {
        "query": query,
        "count": len(products),
        "products": [product.to_tool_dict() for product in products],
    }


BROWSE_PRODUCTS = {
    "name": "browse_products",
    "description": (
        "List products by category and/or price, sorted by price. Use this - not "
        "search_products - whenever the customer asks about the catalog as a whole "
        "rather than a specific item: the cheapest or most expensive piece, everything "
        "under a budget, or all products in a category. Unlike search_products this sees "
        "the entire catalog, so its answer about price ordering is complete and can be "
        "stated with confidence. Categories in this store are: "
        "T-Shirts, Polo Shirts, Hoodies & Sweatshirts, Joggers & Sweatpants, Jackets, Tops."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Product category to restrict to, in English, e.g. 'hoodies'. Omit for the whole catalog.",
            },
            "max_price": {
                "type": "number",
                "description": "Only products at or below this price, in the store's currency.",
            },
            "min_price": {
                "type": "number",
                "description": "Only products at or above this price.",
            },
            "sort": {
                "type": "string",
                "enum": ["price_asc", "price_desc"],
                "description": "price_asc for cheapest first (default), price_desc for most expensive first.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum products to return (1-8). Defaults to 8.",
            },
        },
    },
}


async def _browse_products(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch to ``catalog.service.browse_products``."""
    def number(key):
        value = arguments.get(key)
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    sort = arguments.get("sort")
    sort = sort if sort in ("price_asc", "price_desc") else "price_asc"

    limit = arguments.get("limit")
    try:
        limit = int(limit) if limit is not None else MAX_BROWSE_RESULTS
    except (TypeError, ValueError):
        limit = MAX_BROWSE_RESULTS
    limit = max(1, min(limit, MAX_BROWSE_RESULTS))

    category = str(arguments.get("category") or "").strip() or None

    try:
        products = await catalog_service.browse_products(
            category=category,
            max_price=number("max_price"),
            min_price=number("min_price"),
            sort=sort,
            limit=limit,
        )
    except CatalogUnavailable as exc:
        logger.error("browse_products failed: %s", exc)
        return {"error": "catalog_unavailable"}

    return {
        "category": category,
        "sort": sort,
        "count": len(products),
        "products": [product.to_tool_dict() for product in products],
    }


GET_ORDER_STATUS = {
    "name": "get_order_status",
    "description": (
        "Look up one order by its order number. Both arguments are required: the order "
        "number, and the email address or phone number the customer used when placing "
        "the order. The contact detail proves the order is theirs, so ask for it before "
        "calling this tool - never guess it, and never call this tool with a contact the "
        "customer has not given you in this conversation. Returns the order's status, "
        "items, total and any tracking information. A not_found result means either that "
        "there is no such order or that the contact does not match it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "order_number": {
                "type": "string",
                "description": "The order number, with or without the # sign, e.g. '#1003'.",
            },
            "contact": {
                "type": "string",
                "description": (
                    "The email address or phone number on the order, exactly as the "
                    "customer gave it."
                ),
            },
        },
        "required": ["order_number", "contact"],
    },
}


async def _get_order_status(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch to ``orders.service.get_order_status``."""
    order_number = str(arguments.get("order_number") or "").strip()
    contact = str(arguments.get("contact") or "").strip()
    if not order_number:
        return {"error": "An order number is required."}

    try:
        order = await orders_service.get_order_status(order_number, contact=contact)
    except OrdersUnavailable as exc:
        logger.error("get_order_status failed: %s", exc)
        return {"error": "orders_unavailable"}

    if order is None:
        # Deliberately the same answer whether the order does not exist or the contact
        # did not match it, so this tool cannot be used to find valid order numbers.
        return {"order_number": order_number, "found": False, "error": "not_found"}

    return {"found": True, "order": order.to_tool_dict()}


GET_ORDERS_BY_CUSTOMER = {
    "name": "get_orders_by_customer",
    "description": (
        "List a customer's recent orders using the email address or phone number they "
        "placed them with. Use this when the customer does not remember their order "
        "number. Ask them for the email or phone first - never invent one. Returns the "
        "most recent orders with their status and items."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "contact": {
                "type": "string",
                "description": (
                    "The customer's email address or phone number, exactly as they gave it."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum orders to return (1-5). Defaults to 5.",
            },
        },
        "required": ["contact"],
    },
}


async def _get_orders_by_customer(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch to ``orders.service.get_orders_by_customer``."""
    contact = str(arguments.get("contact") or "").strip()
    if not contact:
        return {"error": "An email address or phone number is required."}

    limit = arguments.get("limit")
    try:
        limit = int(limit) if limit is not None else MAX_ORDER_RESULTS
    except (TypeError, ValueError):
        limit = MAX_ORDER_RESULTS
    limit = max(1, min(limit, MAX_ORDER_RESULTS))

    try:
        orders = await orders_service.get_orders_by_customer(contact, limit=limit)
    except OrdersUnavailable as exc:
        logger.error("get_orders_by_customer failed: %s", exc)
        return {"error": "orders_unavailable"}

    return {
        "count": len(orders),
        # Line items are dropped from a list view: five orders' worth of items would
        # crowd out everything else. get_order_status has the detail.
        "orders": [order.to_tool_dict(include_items=False) for order in orders],
    }


IDENTIFY_PRODUCT_FROM_IMAGE = {
    "name": "identify_product_from_image",
    "description": (
        "Look at the photo the customer attached to this message and find which catalog "
        "products it might be. Call this whenever a customer sends a picture of a "
        "garment - to ask what it is, whether the store sells it, or its price. Takes no "
        "arguments: the photo is taken from the current message. Returns what was seen "
        "in the picture, whether the match is certain, and the matching products with "
        "their real prices, sizes and stock. Only call it when a photo is attached to "
        "the message you are answering now; an image from an earlier message is gone."
    ),
    "parameters": {"type": "object", "properties": {}},
}


async def _identify_product_from_image(
    arguments: Dict[str, Any],
    context: "ToolContext",
) -> Dict[str, Any]:
    """Dispatch to ``catalog.service.identify_product_from_image``."""
    images = context.images
    if not images:
        # The model called this without a photo in the current message - usually because
        # the customer referred back to one it can no longer see.
        return {"error": "no_image_in_this_message"}

    try:
        found = await catalog_service.identify_product_from_image(images)
    except CatalogUnavailable as exc:
        logger.error("identify_product_from_image failed: %s", exc)
        return {"error": "catalog_unavailable"}

    if found.reason:
        # Recorded for the log only. What the bot does about it is set in the prompt;
        # putting the reasoning in the payload would get it read out to the customer.
        logger.info("Identification not asserted (%s)", found.reason)
    return found.to_tool_dict()


GET_DELIVERY_COST = {
    "name": "get_delivery_cost",
    "description": (
        "What the store charges to deliver, read live from its own shipping rates. Call "
        "this whenever a customer asks about delivery cost or shipping, and always "
        "before you read an order back for confirmation - with cash on delivery the "
        "customer hands the courier the price of the goods plus this, so they have to be "
        "told it before they agree. Pass the governorate if you know it. If the result "
        "says unavailable, say you cannot confirm the delivery charge rather than naming "
        "a figure."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "governorate": {
                "type": "string",
                "description": (
                    "The Egyptian governorate or district the order is going to, if the "
                    "customer has said. Omit if not known yet."
                ),
            },
            "order_total": {
                "type": "number",
                "description": (
                    "The total of the goods, if known - some rates depend on it."
                ),
            },
        },
    },
}


async def _get_delivery_cost(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch to ``orders.service.delivery_cost``."""
    governorate = str(arguments.get("governorate") or "").strip() or None
    try:
        total = float(arguments.get("order_total") or 0)
    except (TypeError, ValueError):
        total = 0.0

    try:
        rate = await orders_service.delivery_cost(governorate, subtotal=total)
    except OrdersUnavailable as exc:
        logger.error("get_delivery_cost failed: %s", exc)
        return {"error": "delivery_cost_unavailable"}

    if rate is None:
        return {"available": False, "error": "no_rate_for_this_destination"}

    payload = {
        "available": True,
        "method": rate["title"],
        "cost": rate["amount"] + " " + rate["currency"],
    }
    period = orders_service.delivery_period()
    if period:
        # A customer asking about delivery usually wants both the price and the wait.
        payload["delivery_period"] = period
    if total > 0:
        # The finished figures, so nothing is left for the model to add up or recall.
        # It once reported a delivery charge of its own invention alongside a correct
        # goods price.
        try:
            payload["items_total"] = _money(total, rate["currency"])
            payload["total_with_delivery"] = _money(
                total + float(rate["amount"]), rate["currency"])
        except (TypeError, ValueError):
            pass
    return payload


def _money(amount: float, currency: str) -> str:
    """"580.0" reads badly to a customer; "580 EGP" does not."""
    text = ("%.2f" % float(amount)).rstrip("0").rstrip(".")
    return text + " " + currency


CREATE_COD_ORDER = {
    "name": "create_cod_order",
    "description": (
        "Place a real cash-on-delivery order in the store. The customer pays the courier "
        "in cash on arrival, so no payment is taken here. This creates a genuine order "
        "that staff will pack and ship, so call it only after the customer has told you "
        "every detail below and has explicitly confirmed the order when you read it back "
        "to them. Never invent, assume or complete a name, phone number or address, and "
        "never reuse details from a different conversation. If anything is missing, ask "
        "for it instead of calling this tool. Returns the created order with its number "
        "and total."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "The pieces being ordered.",
                "items": {
                    "type": "object",
                    "properties": {
                        "product": {
                            "type": "string",
                            "description": "The product's exact title or id from a tool result.",
                        },
                        "size": {"type": "string", "description": "Size, e.g. 'L'."},
                        "color": {"type": "string", "description": "Colour, e.g. 'Brown'."},
                        "quantity": {"type": "integer", "description": "How many. Defaults to 1."},
                    },
                    "required": ["product", "size", "color"],
                },
            },
            "customer_name": {"type": "string", "description": "The customer's full name."},
            "phone": {
                "type": "string",
                "description": "The delivery phone number, exactly as the customer gave it.",
            },
            "address1": {
                "type": "string",
                "description": "Street address: building number and street.",
            },
            "address2": {
                "type": "string",
                "description": "Flat, floor or district, if the customer gave one.",
            },
            "city": {
                "type": "string",
                "description": "The town or city, e.g. 'Shibin El Kom' or 'Cairo'.",
            },
            "governorate": {
                "type": "string",
                "description": (
                    "The Egyptian governorate the address is in, e.g. 'Monufia', "
                    "'Cairo', 'Giza' - or the customer's own Arabic spelling. Required: "
                    "an order without it cannot be shipped."
                ),
            },
            "email": {"type": "string", "description": "Email, only if the customer offered one."},
            "note": {
                "type": "string",
                "description": "Any delivery instruction the customer asked to pass on.",
            },
            "customer_confirmed": {
                "type": "boolean",
                "description": (
                    "True only if you read the full order back - items, sizes, colours, "
                    "total, name, phone and address - and the customer agreed to it in "
                    "their reply. Never set this to true in the same message that asks "
                    "for confirmation."
                ),
            },
        },
        "required": ["items", "customer_name", "phone", "address1", "city",
                     "governorate", "customer_confirmed"],
    },
}


async def _create_cod_order(
    arguments: Dict[str, Any],
    context: "ToolContext",
) -> Dict[str, Any]:
    """Dispatch to ``orders.service.create_cod_order``."""
    if not arguments.get("customer_confirmed"):
        # A speed bump, not a guarantee - the model fills this in itself. The real
        # protection is the read-back rule in the prompt and the fact that every detail
        # has to have come from the customer.
        return {"error": "not_confirmed"}

    raw_items = arguments.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return {"error": "An order needs at least one item."}

    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            return {"error": "Each item needs a product, size and colour."}
        items.append(RequestedItem(
            product=str(raw.get("product") or "").strip(),
            size=str(raw.get("size") or "").strip() or None,
            color=str(raw.get("color") or "").strip() or None,
            quantity=raw.get("quantity") or 1,
        ))

    def text(key):
        return str(arguments.get(key) or "").strip() or None

    try:
        order = await orders_service.create_cod_order(
            items=items,
            customer_name=text("customer_name") or "",
            phone=text("phone") or "",
            address1=text("address1") or "",
            address2=text("address2"),
            city=text("city") or "",
            governorate=text("governorate"),
            email=text("email"),
            note=text("note"),
            # From the request, never from the model: this becomes a tag on a real order,
            # and the link back to the chat that produced it.
            channel=context.channel,
            conversation_id=context.conversation_id,
        )
    except OrderRejected as exc:
        logger.warning("COD order refused: %s", exc)
        payload = {"created": False, "error": "rejected", "reason": str(exc)}
        payload.update(exc.detail)
        return payload
    except OrdersUnavailable as exc:
        logger.error("create_cod_order failed: %s", exc)
        return {"created": False, "error": "orders_unavailable"}

    # Note that this conversation is owed feedback on this order - but only once it has
    # actually arrived. Nothing is asked now; see feedback.service.review_due().
    feedback_service.expect_review(context.conversation_id, order.number)

    payload = {"created": True, "order": order.to_tool_dict()}
    period = orders_service.delivery_period()
    if period:
        # Included here so the confirmation and the timing come from one result. Absent
        # when nobody has configured it, and the prompt then says nothing about timing.
        payload["delivery_period"] = period
    return payload


CREATE_PAYMENT_LINK = {
    "name": "create_payment_link",
    "description": (
        "For a customer who wants to pay online by card instead of cash on delivery. "
        "Creates a secure Shopify checkout link for the pieces they chose and returns "
        "it. This does NOT place an order: nothing is ordered, reserved or charged "
        "until the customer opens the link and pays there. Only the items are needed - "
        "the checkout collects the name, phone and address itself, so do not ask for "
        "them first, though you may pass anything the customer has already told you. "
        "Never ask for a card number, CVV, PIN or OTP; the link is the only way payment "
        "is ever taken. Use create_cod_order instead when they want to pay cash on "
        "delivery."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "The pieces the customer wants to pay for.",
                "items": {
                    "type": "object",
                    "properties": {
                        "product": {
                            "type": "string",
                            "description": "The product's exact title or id from a tool result.",
                        },
                        "size": {"type": "string", "description": "Size, e.g. 'L'."},
                        "color": {"type": "string", "description": "Colour, e.g. 'Brown'."},
                        "quantity": {"type": "integer", "description": "How many. Defaults to 1."},
                    },
                    "required": ["product", "size", "color"],
                },
            },
            "customer_name": {
                "type": "string",
                "description": "Their name, only if they have already given it. Optional.",
            },
            "phone": {
                "type": "string",
                "description": "Their phone, only if they have already given it. Optional.",
            },
            "email": {
                "type": "string",
                "description": "Their email, only if they have already given it. Optional.",
            },
            "address1": {
                "type": "string",
                "description": (
                    "Street address, only if they have already given it. Optional - "
                    "the checkout asks for it. Never ask for it just to fill this in."
                ),
            },
            "address2": {"type": "string", "description": "Flat, floor or district. Optional."},
            "city": {"type": "string", "description": "Town or city. Optional."},
            "governorate": {"type": "string", "description": "Governorate. Optional."},
            "note": {
                "type": "string",
                "description": "Any instruction they asked to pass on to the store.",
            },
            "customer_confirmed": {
                "type": "boolean",
                "description": (
                    "True only if you read the pieces back - items, sizes, colours - and "
                    "the customer agreed. Never set this to true in the same message "
                    "that asks for confirmation."
                ),
            },
        },
        "required": ["items", "customer_confirmed"],
    },
}


async def _create_payment_link(
    arguments: Dict[str, Any],
    context: "ToolContext",
) -> Dict[str, Any]:
    """Dispatch to ``orders.service.create_draft_order``."""
    if not arguments.get("customer_confirmed"):
        return {"error": "not_confirmed"}

    raw_items = arguments.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return {"error": "A payment link needs at least one item."}

    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            return {"error": "Each item needs a product, size and colour."}
        items.append(RequestedItem(
            product=str(raw.get("product") or "").strip(),
            size=str(raw.get("size") or "").strip() or None,
            color=str(raw.get("color") or "").strip() or None,
            quantity=raw.get("quantity") or 1,
        ))

    def text(key):
        return str(arguments.get(key) or "").strip() or None

    try:
        draft = await orders_service.create_draft_order(
            items=items,
            customer_name=text("customer_name"),
            phone=text("phone"),
            email=text("email"),
            address1=text("address1"),
            address2=text("address2"),
            city=text("city"),
            governorate=text("governorate"),
            note=text("note"),
            # From the request, never from the model - as with a COD order.
            channel=context.channel,
            conversation_id=context.conversation_id,
        )
    except OrderRejected as exc:
        logger.warning("Payment link refused: %s", exc)
        payload = {"created": False, "error": "rejected", "reason": str(exc)}
        payload.update(exc.detail)
        return payload
    except OrdersUnavailable as exc:
        logger.error("create_payment_link failed: %s", exc)
        return {"created": False, "error": "orders_unavailable"}

    # Deliberately no expect_review here, and no delivery_period: nothing has been
    # ordered yet. Both follow the payment, from payment_news().
    return {"created": True, "payment_link": draft.to_tool_dict()}


CANCEL_ORDER = {
    "name": "cancel_order",
    "description": (
        "Cancel an order that has not shipped yet, and put the items back on sale. This "
        "is real and cannot be undone, so call it only after the customer has asked to "
        "cancel, you have read the order back to them, and they have confirmed. Both "
        "arguments are required: the order number, and the email or phone on the order, "
        "which proves it is theirs. An order that has already shipped cannot be "
        "cancelled - the result will say so, and an exchange is the route instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "order_number": {
                "type": "string",
                "description": "The order number, e.g. '#1008' or '1008'.",
            },
            "contact": {
                "type": "string",
                "description": (
                    "The email address or phone number on the order, exactly as the "
                    "customer gave it in this conversation. Never supply one they have "
                    "not given you."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Why they want to cancel, in their own words, if they said. This "
                    "goes to the store owner, not back to the customer."
                ),
            },
            "customer_confirmed": {
                "type": "boolean",
                "description": (
                    "True only if you read the order back and the customer confirmed "
                    "they want it cancelled."
                ),
            },
        },
        "required": ["order_number", "contact"],
    },
}


async def _cancel_order(
    arguments: Dict[str, Any],
    context: "ToolContext",
) -> Dict[str, Any]:
    """Dispatch to ``orders.service.cancel_order``, then tell the store owner."""
    try:
        order = await orders_service.cancel_order(
            order_number=str(arguments.get("order_number") or ""),
            contact=str(arguments.get("contact") or ""),
            reason_note=str(arguments.get("reason") or ""),
        )
    except CancelRefused as exc:
        # The order exists but a rule stopped it. The bare code tells the model which
        # rule, so it can explain the right thing - shipped is not the same as paid.
        return {"cancelled": False, "error": exc.reason}
    except OrderRejected:
        # Same shape as a wrong contact, deliberately: not_found and not-yours must
        # stay indistinguishable.
        return {"cancelled": False, "error": "not_found"}
    except OrdersUnavailable as exc:
        logger.error("cancel_order failed: %s", exc)
        return {"cancelled": False, "error": "orders_unavailable"}

    # The owner is told every time - their decision. Best-effort: the order is already
    # cancelled, and a mail failure must not make the bot claim otherwise.
    try:
        await notifications_service.notify_order_cancelled(
            order, reason=str(arguments.get("reason") or ""),
            conversation_id=context.conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not email the cancellation of %s", order.number)

    return {
        "cancelled": True,
        # Shopify cancels in a background job, so say what is actually true right now
        # rather than what was asked for.
        "confirmed": order.is_cancelled,
        "order": order.to_tool_dict(include_items=False),
    }


CREATE_SUPPORT_TICKET = {
    "name": "create_support_ticket",
    "description": (
        "Log an issue for a human at the store to deal with, and give the customer a "
        "reference number. Use this when the customer needs something you cannot do: a "
        "damaged or wrong item, a delivery gone wrong, a return or exchange, changing or "
        "cancelling an order, a payment problem, or a complaint. Do not use it for "
        "anything you can answer with the other tools. Ask the customer to describe the "
        "problem in their own words and for an email or phone number to be reached on "
        "before calling this."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": list(CATEGORIES),
                "description": "The kind of issue. Use 'other' if none of them fits.",
            },
            "summary": {
                "type": "string",
                "description": (
                    "What the problem is, in the customer's own words as far as "
                    "possible. Include anything they said that the team will need."
                ),
            },
            "contact": {
                "type": "string",
                "description": (
                    "The email or phone the customer wants a reply on, exactly as given."
                ),
            },
            "customer_name": {"type": "string", "description": "Their name, if given."},
            "order_number": {
                "type": "string",
                "description": "The order this is about, if there is one.",
            },
        },
        "required": ["category", "summary", "contact"],
    },
}


async def _create_support_ticket(
    arguments: Dict[str, Any],
    context: "ToolContext",
) -> Dict[str, Any]:
    """Dispatch to ``support.service.create_ticket``."""
    try:
        ticket = await support_service.create_ticket(
            category=str(arguments.get("category") or "other"),
            summary=str(arguments.get("summary") or ""),
            contact=str(arguments.get("contact") or ""),
            customer_name=str(arguments.get("customer_name") or ""),
            order_number=str(arguments.get("order_number") or "").strip() or None,
            # From the request, not the model: these tie the ticket to a real
            # conversation the store owner can go and read.
            conversation_id=context.conversation_id,
            channel=context.channel,
        )
    except TicketRejected as exc:
        return {"logged": False, "error": "rejected", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - never lose the conversation over this
        logger.exception("create_support_ticket failed")
        return {"logged": False, "error": "ticket_not_saved",
                "reason": type(exc).__name__}

    return {"logged": True, "ticket": ticket.to_tool_dict()}


RECORD_FEEDBACK = {
    "name": "record_feedback",
    "description": (
        "Write down what a customer thinks of a piece they have received - how it fits, "
        "the fabric, the colour, whether it matched the photos - or of the delivery. Use "
        "this for an opinion, not for a problem that needs someone to act: anything the "
        "customer wants fixed, replaced, refunded or chased is a support ticket instead. "
        "Only for goods they actually have; a customer who has not received their order "
        "yet has no opinion of it to record. Never ask a customer to score or rate "
        "anything out of five; record what they said in their own words. Call this once "
        "per conversation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "comment": {
                "type": "string",
                "description": (
                    "What the customer said, in their own words and their own language. "
                    "Do not translate it, tidy it up or summarise it away - this is the "
                    "record."
                ),
            },
            "sentiment": {
                "type": "string",
                "enum": ["positive", "neutral", "negative"],
                "description": (
                    "How the customer sounds about the shop, read from what they wrote. "
                    "Use negative for anything unhappy, disappointed or critical, even "
                    "mildly. If you are unsure, say negative."
                ),
            },
            "customer_name": {
                "type": "string",
                "description": "Their name, if you know it from the conversation.",
            },
            "contact": {
                "type": "string",
                "description": "Email or phone, if they have given one. Never ask for it just to record feedback.",
            },
            "order_number": {
                "type": "string",
                "description": (
                    "The order the pieces came from. Pass it whenever you know it - it "
                    "is what ties the opinion to what they actually received."
                ),
            },
        },
        "required": ["comment", "sentiment"],
    },
}


async def _record_feedback(
    arguments: Dict[str, Any],
    context: "ToolContext",
) -> Dict[str, Any]:
    """Dispatch to ``feedback.service.record_feedback``."""
    try:
        feedback = await feedback_service.record_feedback(
            comment=str(arguments.get("comment") or ""),
            sentiment=str(arguments.get("sentiment") or ""),
            customer_name=str(arguments.get("customer_name") or ""),
            contact=str(arguments.get("contact") or ""),
            order_number=str(arguments.get("order_number") or "").strip() or None,
            # From the request, not the model - same reason as the ticket above.
            conversation_id=context.conversation_id,
            channel=context.channel,
        )
    except FeedbackRejected as exc:
        return {"recorded": False, "error": "rejected", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - never lose the conversation over this
        logger.exception("record_feedback failed")
        return {"recorded": False, "error": "feedback_not_saved",
                "reason": type(exc).__name__}

    return {"recorded": True, "feedback": feedback.to_tool_dict()}


# Registry: tool name -> (declaration, handler). Each build step adds one entry.
_REGISTRY: Dict[str, Dict[str, Any]] = {
    SEARCH_PRODUCTS["name"]: {"declaration": SEARCH_PRODUCTS, "handler": _search_products},
    BROWSE_PRODUCTS["name"]: {"declaration": BROWSE_PRODUCTS, "handler": _browse_products},
    GET_ORDER_STATUS["name"]: {"declaration": GET_ORDER_STATUS, "handler": _get_order_status},
    GET_ORDERS_BY_CUSTOMER["name"]: {
        "declaration": GET_ORDERS_BY_CUSTOMER,
        "handler": _get_orders_by_customer,
    },
    GET_DELIVERY_COST["name"]: {
        "declaration": GET_DELIVERY_COST,
        "handler": _get_delivery_cost,
    },
    IDENTIFY_PRODUCT_FROM_IMAGE["name"]: {
        "declaration": IDENTIFY_PRODUCT_FROM_IMAGE,
        "handler": _identify_product_from_image,
        # Handed the turn's context as well as the model's arguments.
        "wants_context": True,
    },
    CREATE_COD_ORDER["name"]: {
        "declaration": CREATE_COD_ORDER,
        "handler": _create_cod_order,
        "wants_context": True,
    },
    CREATE_PAYMENT_LINK["name"]: {
        "declaration": CREATE_PAYMENT_LINK,
        "handler": _create_payment_link,
        "wants_context": True,
        # Not offered at all when the store takes cash only, so the model cannot promise
        # a way to pay that does not exist.
        "available": lambda: settings.online_payment_configured,
    },
    CANCEL_ORDER["name"]: {
        "declaration": CANCEL_ORDER,
        "handler": _cancel_order,
        "wants_context": True,
    },
    CREATE_SUPPORT_TICKET["name"]: {
        "declaration": CREATE_SUPPORT_TICKET,
        "handler": _create_support_ticket,
        "wants_context": True,
    },
    RECORD_FEEDBACK["name"]: {
        "declaration": RECORD_FEEDBACK,
        "handler": _record_feedback,
        "wants_context": True,
    },
}

Handler = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


def _available(entry: Dict[str, Any]) -> bool:
    """Whether this tool is offered at all right now.

    A tool can depend on configuration - online payment is off for a cash-only store -
    and an unavailable one is never declared, so the model cannot offer the customer
    something the shop does not do. Checked on every call rather than at import, so a
    setting change takes effect without a restart.
    """
    check = entry.get("available")
    return True if check is None else bool(check())


def declarations() -> List[Dict[str, Any]]:
    """Every tool declaration currently on offer, for handing to the model."""
    return [entry["declaration"] for entry in _REGISTRY.values() if _available(entry)]


def names() -> List[str]:
    return [name for name, entry in _REGISTRY.items() if _available(entry)]


async def dispatch(
    name: str,
    arguments: Dict[str, Any],
    context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Run one tool call and return its result as a JSON-serialisable dict.

    ``context`` carries facts about the turn - the attached photo, the channel. Only the
    tools that ask for it receive it, so a photo cannot leak into a tool that has no
    business with it.
    """
    entry = _REGISTRY.get(name)
    if entry is not None and not _available(entry):
        # Switched off between the declaration and the call, or hallucinated from an
        # older prompt. Either way it must not run.
        logger.warning("Model called %r, which is not currently available", name)
        return {"error": "The " + name + " tool is not available."}
    if entry is None:
        # A hallucinated tool name is reported back rather than raised, so the model can
        # correct itself instead of the whole turn failing.
        logger.warning("Model called an unknown tool: %r", name)
        return {"error": "There is no tool named " + repr(name) + "."}

    logger.info("Tool call: %s(%s)", name, arguments)
    handler: Handler = entry["handler"]
    try:
        if entry.get("wants_context"):
            return await handler(arguments or {}, context or ToolContext())
        return await handler(arguments or {})
    except Exception as exc:  # noqa: BLE001 - a tool must never break the conversation
        logger.exception("Tool %s raised", name)
        return {"error": "The " + name + " tool failed: " + type(exc).__name__ + "."}
