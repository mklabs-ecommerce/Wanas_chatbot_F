"""Orders module - the public surface for anything about a customer's orders.

Read-only for now: looking an order up by number, or listing a customer's recent orders.
Order *creation* (draft orders and cash on delivery) arrives in later build steps.

Two things this module owns that the raw Shopify client deliberately does not:

1. **Turning Shopify order JSON into our ``Order`` shape.** Nothing outside this module
   parses that payload.
2. **Deciding who is allowed to see an order.** Order numbers are sequential and trivial
   to guess (#1001, #1002, ...), and an order record carries a name, a phone number, a
   delivery city and a purchase history. So a lookup by number alone is not enough: the
   customer must also give the email or phone on the order. Shopify's own search cannot
   be trusted for that check - ``phone:01067177129`` happily returns orders belonging to
   ``+201000000000`` - so the match is made here, on normalised values.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from app.core.config import settings
from app.integrations.shopify.client import (
    ShopifyClient,
    ShopifyError,
    ShopifyRejected,
)
from app.modules.catalog import service as catalog_service
from app.modules.catalog.service import CatalogUnavailable
from app.modules.orders import governorates, shipping
from app.modules.orders.schemas import LineItem, Order, Tracking

logger = logging.getLogger(__name__)

_client: Optional[ShopifyClient] = None

# How many orders to pull when listing a customer's history.
DEFAULT_HISTORY_LIMIT = 5
# Shopify's loose matching means a contact search returns near-misses, which are then
# filtered out here. Fetch a few extra so genuine matches are not lost to that filter.
_SEARCH_OVERFETCH = 4

# Tags that mark an order as cash on delivery. The store's existing orders use
# "cash-on-delivery"; the exact convention is settled in build step 8.
_COD_TAGS = {"cash-on-delivery", "cash_on_delivery", "cash on delivery", "cod"}

# Anything outside this set is stripped before a value reaches Shopify's search syntax,
# so a customer's input cannot alter the query it is interpolated into.
_SAFE_QUERY_CHARS = re.compile(r"[^A-Za-z0-9@._+#\-]")

# Arabic-Indic and Persian digits, so "٠١٠٦٧١٧٧١٢٩" compares equal to "01067177129".
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# How many trailing digits of a phone number must agree. Ten covers a full Egyptian
# mobile number without its country code or leading zero (1067177129), so it is strict
# enough that two customers cannot collide, and lenient enough that +20, 0020, 0 and
# bare-number spellings of the same phone all match.
_PHONE_KEY_DIGITS = 10


class OrdersUnavailable(Exception):
    """Orders could not be read. Callers should say so, never invent an order."""


def _shopify() -> ShopifyClient:
    global _client
    if _client is None:
        _client = ShopifyClient()
    return _client


# --- public API -----------------------------------------------------------


async def get_order_status(
    order_number: str,
    contact: Optional[str] = None,
) -> Optional[Order]:
    """Return one order by its number, if ``contact`` proves the asker owns it.

    ``contact`` is the email address or phone number on the order. Returns ``None`` both
    when no such order exists and when the contact does not match - the caller cannot
    tell the two apart, so this cannot be used to discover which order numbers are real.
    """
    wanted = _order_number_key(order_number)
    if not wanted:
        return None

    nodes = await _fetch("name:#" + wanted, first=5)

    for node in nodes:
        order = _to_order(node)
        # Shopify's `name:` filter also matches partially, so confirm the number itself.
        if _order_number_key(order.number) != wanted:
            continue
        if not _authorised(order, contact):
            logger.info(
                "Order %s lookup refused: contact did not match", order.number
            )
            return None
        return order

    return None


async def get_orders_by_customer(
    contact: str,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> List[Order]:
    """Return a customer's recent orders, newest first.

    ``contact`` is an email address or phone number, and is self-verifying: only orders
    that genuinely carry it are returned. Shopify's own filter matches loosely, so its
    results are re-checked here before anything is handed back.
    """
    contact = (contact or "").strip()
    if not contact:
        return []

    limit = max(1, min(limit, 20))
    field = "email" if "@" in contact else "phone"
    safe = _SAFE_QUERY_CHARS.sub("", contact)
    if not safe:
        return []

    nodes = await _fetch(field + ":" + safe, first=limit + _SEARCH_OVERFETCH)

    matched = []
    for node in nodes:
        order = _to_order(node)
        if _contact_matches(order, contact):
            matched.append(order)
        else:
            # Expected and harmless, but worth seeing in the log: it is the reason this
            # filter exists rather than trusting Shopify's result set.
            logger.debug("Discarded loosely-matched order %s", order.number)

    return matched[:limit]


async def lookup_for_staff(order_number: str) -> Optional[Order]:
    """Read an order without the customer contact check, for a store-side reader.

    ``get_order_status()`` refuses to return an order unless the asker proves it is
    theirs, because there the asker is whoever happens to be holding the chat window.
    That check is meaningless for the store's own staff notification: the owner is
    entitled to their own orders, and a customer who mistypes their email in a complaint
    would otherwise produce a support email with the order silently missing.

    **Never expose this as a tool.** Nothing the model can call may reach it - the whole
    point of the contact check is that order numbers are sequential and guessable.
    """
    wanted = _order_number_key(order_number)
    if not wanted:
        return None

    for node in await _fetch("name:#" + wanted, first=5):
        order = _to_order(node)
        # Shopify's `name:` filter matches loosely - "#100" also returns "#1003".
        if _order_number_key(order.number) == wanted:
            return order
    return None


def contact_matches(order: Order, contact: Optional[str]) -> bool:
    """Whether ``contact`` is the email or phone recorded on ``order``.

    Public because a staff-side reader wants the answer as information rather than as a
    gate: a complaint quoting an order with a contact that does not match it is worth
    flagging to a person, not worth hiding the order over.
    """
    return _contact_matches(order, contact or "")


def admin_url(order: Order) -> Optional[str]:
    """Deep link to this order in the Shopify admin, so staff can act in one click.

    Shopify ids arrive as ``gid://shopify/Order/123``; the admin wants the number.
    """
    numeric = (order.id or "").rsplit("/", 1)[-1]
    store = (settings.shopify_store or "").split(".")[0]
    if not (numeric.isdigit() and store):
        return None
    return "https://admin.shopify.com/store/" + store + "/orders/" + numeric


async def delivery_cost(
    governorate: Optional[str] = None,
    subtotal: float = 0.0,
) -> Optional[Dict[str, str]]:
    """What the store charges to deliver, so it can be quoted before an order exists.

    ``None`` when no rate covers the destination, or none can be read - the caller must
    then say it cannot quote rather than guess a figure the courier will not collect.
    """
    province = governorates.resolve(governorate)
    if governorate and province is None:
        logger.info("Delivery quote asked for unknown governorate %r", governorate)

    try:
        rate = await shipping.rate_for(_shopify(), province, subtotal)
    except ShopifyError as exc:
        logger.warning("Could not read the delivery rate: %s", exc)
        return None

    if rate is None:
        if settings.cod_shipping_fee is None:
            return None
        return {"title": settings.cod_shipping_title,
                "amount": _trim(str(settings.cod_shipping_fee)),
                "currency": settings.store_currency}

    return {"title": rate.title, "amount": _trim(rate.amount), "currency": rate.currency}


# --- authorisation --------------------------------------------------------


def _authorised(order: Order, contact: Optional[str]) -> bool:
    """Whether the asker has proved they own this order."""
    if not settings.orders_require_contact_verification:
        return True
    if not contact:
        return False
    return _contact_matches(order, contact)


def _contact_matches(order: Order, contact: str) -> bool:
    """True when ``contact`` is the email or phone recorded on the order."""
    contact = (contact or "").strip()
    if not contact:
        return False

    if "@" in contact:
        given = _email_key(contact)
        return bool(given) and given == _email_key(order.email)

    given = _phone_key(contact)
    return bool(given) and given == _phone_key(order.phone)


def _email_key(value: Optional[str]) -> str:
    return (value or "").strip().casefold()


def _phone_key(value: Optional[str]) -> str:
    """The comparable tail of a phone number, or "" when there is too little to compare.

    Country code, leading zeros, spaces, dashes and brackets all vary between how a
    customer types their number and how Shopify stored it, so only the significant
    trailing digits are compared.
    """
    digits = re.sub(r"\D", "", (value or "").translate(_ARABIC_DIGITS))
    if len(digits) < _PHONE_KEY_DIGITS:
        return ""
    return digits[-_PHONE_KEY_DIGITS:]


def _order_number_key(value: Optional[str]) -> str:
    """Just the digits of an order number, so "#1003", "1003" and "رقم 1003" agree."""
    return re.sub(r"\D", "", (value or "").translate(_ARABIC_DIGITS))


# --- Shopify ---------------------------------------------------------------


async def _fetch(query: str, first: int) -> List[Dict[str, Any]]:
    """One Shopify order search, with failures turned into ``OrdersUnavailable``.

    Unlike the catalog there is no cache to fall back on: an order's status is exactly
    the kind of thing that must never be answered from a stale copy.
    """
    try:
        return await _shopify().fetch_orders(query=query, first=first)
    except ShopifyError as exc:
        logger.error("Order lookup failed (%s): %s", query, exc)
        raise OrdersUnavailable(str(exc)) from exc


def _to_order(node: Dict[str, Any]) -> Order:
    """Map one raw Shopify order node onto our ``Order``."""
    total, currency = _money(node.get("totalPriceSet"))
    subtotal, _ = _money(node.get("subtotalPriceSet"))
    delivery, _ = _money(node.get("totalShippingPriceSet"))
    address = node.get("shippingAddress") or {}
    customer = node.get("customer") or {}
    tags = [str(tag).strip().lower() for tag in (node.get("tags") or [])]

    tracking, estimated = _shipment(node.get("fulfillments") or [])

    return Order(
        id=str(node.get("id") or ""),
        number=str(node.get("name") or ""),
        placed_on=_date(node.get("createdAt")),
        financial_status=str(node.get("displayFinancialStatus") or ""),
        fulfillment_status=str(node.get("displayFulfillmentStatus") or ""),
        total=_trim(total),
        currency=currency,
        subtotal=_trim(subtotal),
        delivery=_trim(delivery),
        delivery_title=str((node.get("shippingLine") or {}).get("title") or ""),
        cancelled_at=_date(node.get("cancelledAt")),
        cancel_reason=node.get("cancelReason"),
        # Any of the three may be the one the customer gives, so all are considered.
        email=node.get("email") or customer.get("email"),
        phone=node.get("phone") or customer.get("phone") or address.get("phone"),
        ships_to_city=address.get("city"),
        ships_to_country=address.get("country"),
        cash_on_delivery=any(tag in _COD_TAGS for tag in tags),
        estimated_delivery=estimated,
        items=[
            LineItem(
                title=str(item.get("title") or ""),
                quantity=int(item.get("quantity") or 0),
                variant_title=item.get("variantTitle"),
                sku=item.get("sku"),
            )
            for item in ((node.get("lineItems") or {}).get("nodes") or [])
        ],
        tracking=tracking,
    )


def _shipment(fulfillments: Sequence[Dict[str, Any]]):
    """Tracking and estimated delivery from the most recent fulfillment that has any."""
    for fulfillment in fulfillments:
        for info in fulfillment.get("trackingInfo") or []:
            tracking = Tracking(
                number=info.get("number"),
                url=info.get("url"),
                company=info.get("company"),
            )
            if not tracking.is_empty:
                return tracking, _date(fulfillment.get("estimatedDeliveryAt"))
    for fulfillment in fulfillments:
        if fulfillment.get("estimatedDeliveryAt"):
            return None, _date(fulfillment.get("estimatedDeliveryAt"))
    return None, None


def _money(price_set: Optional[Dict[str, Any]]):
    money = (price_set or {}).get("shopMoney") or {}
    return str(money.get("amount") or ""), str(money.get("currencyCode") or "")


def _date(timestamp: Optional[str]) -> str:
    """Shopify's ISO timestamp reduced to a plain date; the time of day is noise here."""
    if not timestamp:
        return ""
    return str(timestamp)[:10]


def _trim(amount: str) -> str:
    """"650.0" -> "650", keeping genuine decimals."""
    text = str(amount or "")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


# --- creating a cash-on-delivery order ------------------------------------
#
# COD is the common path in Egypt, and unlike online payment it has no checkout step: the
# order is created outright through the Admin API, so everything a checkout page would
# normally collect and validate has to be collected in conversation and validated here.
#
# The store's convention, confirmed by the owner and matching the orders already in the
# shop: financial status PENDING, tagged so staff can filter for cash to collect. The
# channel tag follows the conversation the order actually came from - the existing orders
# say "whatsapp" because that is where they came from, and repeating it on a web order
# would misfile it.


@dataclass
class RequestedItem:
    """One line a customer asked for, before it is matched to a real variant."""

    product: str
    size: Optional[str] = None
    color: Optional[str] = None
    quantity: int = 1


class OrderRejected(Exception):
    """The order cannot be created as asked, and the customer needs to hear why.

    Distinct from ``OrdersUnavailable``: nothing is broken, the request is simply not
    fillable. ``detail`` carries what is available instead, when that is known.
    """

    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.detail = detail or {}


MAX_ITEMS_PER_ORDER = 5
MAX_QUANTITY_PER_ITEM = 10


async def create_cod_order(
    items: Sequence[RequestedItem],
    customer_name: str,
    phone: str,
    address1: str,
    city: str,
    governorate: Optional[str] = None,
    address2: Optional[str] = None,
    email: Optional[str] = None,
    note: Optional[str] = None,
    channel: str = "web",
) -> Order:
    """Create a real cash-on-delivery order in Shopify.

    Every detail must come from the customer: nothing here is inferred or defaulted on
    their behalf. The variants are resolved from the live catalog rather than trusted
    from the caller, so an order can never be placed for a size that does not exist or
    has just sold out.
    """
    items = list(items or [])
    if not items:
        raise OrderRejected("No items were given for the order.")
    if len(items) > MAX_ITEMS_PER_ORDER:
        raise OrderRejected("An order may have at most "
                            + str(MAX_ITEMS_PER_ORDER) + " different items.")

    customer_name = (customer_name or "").strip()
    address1 = (address1 or "").strip()
    city = (city or "").strip()
    phone = (phone or "").strip()

    # Delivery is the whole point of cash on delivery; a missing field here means a
    # parcel that cannot be delivered and cash that cannot be collected.
    for value, label in ((customer_name, "the customer's name"), (phone, "a phone number"),
                         (address1, "a street address"), (city, "a city")):
        if not value:
            raise OrderRejected("The order needs " + label + ".")
    if not _phone_key(phone):
        raise OrderRejected("That phone number does not look complete.")
    if len(customer_name) < 2 or not any(ch.isalpha() for ch in customer_name):
        raise OrderRejected("That name does not look like a name.")

    lines = []
    for item in items:
        quantity = _quantity(item.quantity)
        try:
            product, variant = await catalog_service.resolve_variant(
                item.product, size=item.size, color=item.color)
        except catalog_service.VariantNotFound as exc:
            raise OrderRejected(str(exc), {"available": exc.available}) from exc
        except CatalogUnavailable as exc:
            raise OrdersUnavailable(str(exc)) from exc
        lines.append((product, variant, quantity))

    existing = await _recent_duplicate(phone, lines)
    if existing is not None:
        # Almost always the model calling twice within one turn rather than a customer
        # ordering the same thing again. Returning the first order is the safe reading:
        # a duplicate parcel costs the store real money.
        logger.warning("Reusing order %s instead of creating a duplicate", existing.number)
        return existing

    payload = await _order_payload(lines, customer_name, phone, address1, address2, city,
                                   governorate, email, note, channel)
    customer_id = await _customer_id(customer_name, to_e164(phone), email)
    if customer_id:
        # Associate rather than upsert: Shopify rejects an upsert that carries only a
        # phone, which is all a cash-on-delivery customer usually gives.
        payload["customer"] = {"toAssociate": {"id": customer_id}}
    logger.info("Creating COD order: %s x%d for %s",
                ", ".join(v.title for _p, v, _q in lines), len(lines), _redact(phone))

    try:
        node = await _shopify().create_order(payload, {
            "sendReceipt": settings.cod_send_receipt,
            # Take the stock, but never below what the store allows - if it sold out
            # between the search and the confirmation, this fails instead of overselling.
            "inventoryBehaviour": settings.cod_inventory_behaviour,
        })
    except ShopifyRejected as exc:
        logger.error("Shopify refused the COD order: %s", exc)
        raise OrderRejected("The order could not be placed: " + str(exc)) from exc
    except ShopifyError as exc:
        logger.error("COD order creation failed: %s", exc)
        raise OrdersUnavailable(str(exc)) from exc

    order = _to_order(node)
    logger.info("Created COD order %s (%s %s)", order.number, order.total, order.currency)
    return order


def _quantity(value: Any) -> int:
    try:
        quantity = int(value)
    except (TypeError, ValueError) as exc:
        raise OrderRejected("The quantity must be a whole number.") from exc
    if quantity < 1:
        raise OrderRejected("The quantity must be at least 1.")
    if quantity > MAX_QUANTITY_PER_ITEM:
        raise OrderRejected("At most " + str(MAX_QUANTITY_PER_ITEM)
                            + " of one item can be ordered through the chat.")
    return quantity


async def _order_payload(lines, customer_name, phone, address1, address2, city,
                         governorate, email, note, channel) -> Dict[str, Any]:
    """Build the Shopify input. Prices are never sent - the variant carries its own."""
    first, _, last = customer_name.partition(" ")
    tags = list(settings.cod_order_tags)
    channel = (channel or "").strip().lower()
    if channel and channel not in tags:
        # So the store can see where an order actually came from (Section 1, item 7).
        tags.append(channel)

    # Shopify validates this and refuses anything that is not international form.
    dialable = to_e164(phone)
    surname = last.strip() or first
    address = {
        "firstName": first,
        "lastName": surname,
        "address1": address1,
        "city": city,
        "countryCode": settings.store_country_code,
        "phone": dialable,
    }
    if address2:
        address["address2"] = address2

    # Without this the order reaches staff reading "Government is missing", and nothing
    # can be shipped until someone fills it in by hand.
    province = governorates.resolve(governorate) or governorates.resolve(city)
    if province:
        address["provinceCode"] = province
    elif governorate:
        logger.warning("Unrecognised governorate %r - order will have none", governorate)

    payload: Dict[str, Any] = {
        # requiresShipping has to be said explicitly: orderCreate defaults it to false,
        # so the order arrives claiming it needs no delivery even though every line is a
        # physical garment.
        "lineItems": [{"variantId": variant.id, "quantity": quantity,
                       "requiresShipping": True}
                      for _product, variant, quantity in lines],
        "shippingAddress": address,
        "billingAddress": dict(address),
        "phone": dialable,
        # Not paid yet: the money arrives with the courier.
        "financialStatus": "PENDING",
        "tags": tags,
        "note": _staff_note(channel, phone, note),
    }
    if email:
        payload["email"] = email
    line = await _shipping_line(address.get("provinceCode"), _subtotal(lines))
    if line is not None:
        # A named delivery method, so the Delivery section shows what the courier is
        # doing and what it costs, rather than nothing at all.
        payload["shippingLines"] = [line]
    return payload


def _subtotal(lines) -> float:
    """What the goods come to, before delivery - some rates depend on it."""
    total = 0.0
    for _product, variant, quantity in lines:
        try:
            total += float(variant.price) * quantity
        except (TypeError, ValueError):
            continue
    return total


async def _shipping_line(province: Optional[str], subtotal: float) -> Optional[Dict[str, Any]]:
    """The delivery line to put on the order: the store's own rate where possible."""
    rate = await shipping.rate_for(_shopify(), province, subtotal)
    if rate is not None:
        return {
            "title": rate.title,
            "priceSet": {"shopMoney": {"amount": rate.amount,
                                       "currencyCode": rate.currency}},
        }

    if settings.cod_shipping_fee is None:
        # Nothing readable and nothing configured. Better an empty Delivery section than
        # a charge nobody agreed to.
        logger.warning("No delivery rate available; the order will carry no delivery line")
        return None

    return {
        "title": settings.cod_shipping_title,
        "priceSet": {"shopMoney": {"amount": str(settings.cod_shipping_fee),
                                   "currencyCode": settings.store_currency}},
    }


async def _customer_id(name: str, phone: str, email: Optional[str]) -> Optional[str]:
    """Find this customer, or create them, and return the Shopify id.

    Without this the order is filed under "No customer" and the shop cannot see that the
    person has ordered before. A failure here never costs the order - an order with no
    customer attached is still an order.
    """
    first, _, last = (name or "").partition(" ")
    surname = last.strip() or first

    try:
        found = await _find_customer(phone, email)
        if found:
            return found
        created = await _shopify().create_customer(_customer_input(first, surname, phone, email))
        logger.info("Created a customer record for %s", _redact(phone))
        return created.get("id")
    except ShopifyError as exc:
        logger.warning("Could not attach a customer to this order: %s", exc)
        return None


def _customer_input(first: str, last: str, phone: str,
                    email: Optional[str]) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {"firstName": first, "lastName": last, "phone": phone}
    if email:
        attributes["email"] = email
    return attributes


async def _find_customer(phone: str, email: Optional[str]) -> Optional[str]:
    """An existing customer with this phone or email, verified rather than trusted.

    Shopify's customer search matches as loosely as its order search, so a returned row
    is only accepted when the contact really is the same.
    """
    for value, field in ((email, "email"), (phone, "phone")):
        if not value:
            continue
        safe = _SAFE_QUERY_CHARS.sub("", value)
        if not safe:
            continue
        for node in await _shopify().find_customers(field + ":" + safe):
            node_phone = (node.get("defaultPhoneNumber") or {}).get("phoneNumber")
            node_email = (node.get("defaultEmailAddress") or {}).get("emailAddress")
            if field == "email":
                if _email_key(node_email) == _email_key(value):
                    return node.get("id")
            elif _phone_key(node_phone) == _phone_key(value):
                return node.get("id")
    return None


def _staff_note(channel: str, phone: str, customer_note: Optional[str]) -> str:
    """A line for whoever picks and delivers the parcel."""
    parts = ["Chatbot order (" + (channel or "web") + "). Cash on delivery.",
             # As the customer gave it, which is how staff will hear it read back.
             "Phone: " + phone]
    if customer_note:
        parts.append("Customer note: " + customer_note.strip()[:300])
    return " ".join(parts)


# A local Egyptian mobile is ten digits once the leading zero is dropped (1067177129).
_NATIONAL_DIGITS = 10


def to_e164(phone: str, dial_code: Optional[str] = None) -> str:
    """Put a phone number into the international form Shopify insists on.

    Customers write their own number the local way - "01067177129" - and Shopify rejects
    it with "Phone is invalid". It wants "+201067177129". Every spelling in between
    (00 20..., a bare ten digits, Arabic-Indic digits) leads to the same result.
    """
    raw = (phone or "").strip().translate(_ARABIC_DIGITS)
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""

    dial = re.sub(r"\D", "", dial_code or settings.store_dial_code or "")

    if raw.lstrip().startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if dial and digits.startswith(dial) and len(digits) == len(dial) + _NATIONAL_DIGITS:
        return "+" + digits
    if digits.startswith("0"):
        return "+" + dial + digits[1:]
    return "+" + dial + digits


def _redact(phone: str) -> str:
    """Enough of a number to match it in a log, not enough to be a contact detail."""
    digits = re.sub(r"\D", "", phone or "")
    return "..." + digits[-4:] if len(digits) >= 4 else "..."


async def _recent_duplicate(phone: str, lines) -> Optional[Order]:
    """An order for the same phone and the same items, placed moments ago.

    Checked against Shopify rather than in memory so it still holds after a restart and
    across workers.
    """
    window = settings.cod_duplicate_window_seconds
    if window <= 0:
        return None

    safe = _SAFE_QUERY_CHARS.sub("", phone)
    if not safe:
        return None
    try:
        nodes = await _shopify().fetch_orders(query="phone:" + safe, first=5)
    except ShopifyError as exc:
        # Not worth failing the order over; the duplicate check is a safeguard, not a
        # precondition.
        logger.warning("Could not check for a duplicate order: %s", exc)
        return None

    wanted = sorted((variant.title, quantity) for _p, variant, quantity in lines)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window)

    for node in nodes:
        order = _to_order(node)
        if order.is_cancelled or not _contact_matches(order, phone):
            continue
        created = _parse_timestamp(node.get("createdAt"))
        if created is None or created < cutoff:
            continue
        found = sorted((item.variant_title or item.title, item.quantity)
                       for item in order.items)
        if found == wanted:
            return order
    return None


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
