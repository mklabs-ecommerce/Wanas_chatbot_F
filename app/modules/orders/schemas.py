"""Data shapes the orders module exposes.

As with the catalog, these are the module's public vocabulary - nothing outside this
module handles Shopify's raw order JSON.

``to_tool_dict()`` is where the privacy decision lives: an order record holds far more
than a customer needs to hear back, and some of it (internal notes, staff tags, the
Shopify id) must never reach the chat window. Only the fields listed here are ever shown.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Shopify's fulfillment vocabulary in words a customer understands. Left as-is when a
# status appears that we have not seen, so a new Shopify state degrades to jargon rather
# than to a wrong reassurance.
_FULFILLMENT_WORDS = {
    "UNFULFILLED": "not shipped yet",
    "PARTIALLY_FULFILLED": "partly shipped",
    "FULFILLED": "shipped",
    "IN_PROGRESS": "being prepared",
    "ON_HOLD": "on hold",
    "SCHEDULED": "scheduled for shipping",
    "RESTOCKED": "returned to stock",
}


@dataclass
class LineItem:
    """One product line on an order."""

    title: str
    quantity: int
    variant_title: Optional[str] = None
    sku: Optional[str] = None

    def to_tool_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"title": self.title, "quantity": self.quantity}
        if self.variant_title:
            # Shopify's variant title is "M / Black"; the model can read it as-is.
            payload["variant"] = self.variant_title
        return payload


@dataclass
class Tracking:
    """Carrier tracking for a shipped order."""

    number: Optional[str] = None
    url: Optional[str] = None
    company: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return not (self.number or self.url or self.company)

    def to_tool_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if self.company:
            payload["carrier"] = self.company
        if self.number:
            payload["number"] = self.number
        if self.url:
            payload["url"] = self.url
        return payload


@dataclass
class Order:
    """An order, reduced to what the bot may discuss."""

    id: str
    number: str
    placed_on: str = ""
    financial_status: str = ""
    fulfillment_status: str = ""
    total: str = ""
    currency: str = ""
    # What the goods came to, and what delivery was charged on top. Kept apart because a
    # cash-on-delivery customer is handing over the sum of both at the door.
    subtotal: str = ""
    delivery: str = ""
    delivery_title: str = ""
    cancelled_at: Optional[str] = None
    cancel_reason: Optional[str] = None
    # Contact details are carried so the service can verify who is asking. They are
    # never returned to the model - the customer already knows their own phone number,
    # and echoing it back would leak it to whoever else is holding the chat window.
    email: Optional[str] = None
    phone: Optional[str] = None
    ships_to_city: Optional[str] = None
    ships_to_country: Optional[str] = None
    cash_on_delivery: bool = False
    estimated_delivery: Optional[str] = None
    # The carrier's own report that the parcel was handed over, when there is one. Not a
    # status this app or the store sets - only a courier that reports back fills it in.
    carrier_delivered: bool = False
    items: List[LineItem] = field(default_factory=list)
    tracking: Optional[Tracking] = None

    @property
    def is_cancelled(self) -> bool:
        return bool(self.cancelled_at)

    @property
    def reached_the_customer(self) -> bool:
        """Whether the goods are demonstrably in the customer's hands.

        Deliberately strict, because this is what decides whether the bot may ask someone
        how a garment they are holding turned out. Getting it wrong means asking about a
        parcel that has not arrived.

        For cash on delivery the answer is exact: the customer pays the courier at the
        door, so ``PAID`` cannot happen before the parcel does. ``FULFILLED`` is not
        good enough - it only means the order left the shop.

        An order paid online is ``PAID`` from the moment of checkout, so payment says
        nothing at all about delivery. The only fact left is the carrier's own report,
        which Shopify carries as ``deliveredAt``/``DELIVERED`` - and which stays empty
        unless the courier integrates with the shop. So a prepaid order sent by a courier
        that reports nothing simply never counts as arrived, and the bot never asks how
        it was. That is the intended failure: silence rather than asking someone about a
        parcel they may not have.
        """
        if self.is_cancelled:
            return False
        if not self.cash_on_delivery:
            return self.carrier_delivered
        return self.financial_status.upper() == "PAID"

    @property
    def status_words(self) -> str:
        """The fulfillment state in plain language."""
        if self.is_cancelled:
            return "cancelled"
        return _FULFILLMENT_WORDS.get(
            self.fulfillment_status, self.fulfillment_status.lower().replace("_", " ")
        )

    def to_tool_dict(self, include_items: bool = True) -> Dict[str, Any]:
        """Compact JSON for the model, carrying only what a customer may be told."""
        payload: Dict[str, Any] = {
            "order_number": self.number,
            "placed_on": self.placed_on,
            "status": self.status_words,
            "payment_status": self.financial_status.lower().replace("_", " "),
            "total": (self.total + " " + self.currency).strip(),
        }
        if self.is_cancelled:
            payload["cancelled_on"] = self.cancelled_at
        if self.cash_on_delivery:
            payload["payment_method"] = "cash on delivery"
        if self.delivery:
            payload["delivery"] = (self.delivery + " " + self.currency).strip()
            if self.subtotal:
                payload["items_total"] = (self.subtotal + " " + self.currency).strip()
        if include_items and self.items:
            payload["items"] = [item.to_tool_dict() for item in self.items]
        if self.ships_to_city or self.ships_to_country:
            # City and country only. The street address adds nothing to a status answer
            # and is the most sensitive line in the record.
            payload["ships_to"] = ", ".join(
                part for part in (self.ships_to_city, self.ships_to_country) if part
            )
        if self.estimated_delivery:
            payload["estimated_delivery"] = self.estimated_delivery
        if self.tracking and not self.tracking.is_empty:
            payload["tracking"] = self.tracking.to_tool_dict()
        return payload


@dataclass
class Draft:
    """A basket with a payment link. Not an order, and deliberately a separate type.

    A draft order is what online payment goes through: the pieces are priced and held in
    a checkout the customer opens themselves, and nothing exists in the shop until they
    pay. Keeping it distinct from ``Order`` is the point - code that has a ``Draft``
    cannot accidentally tell a customer their order is placed.
    """

    id: str
    name: str
    checkout_url: str
    status: str = "OPEN"
    total: str = "0"
    subtotal: str = "0"
    delivery: str = "0"
    currency: str = "EGP"
    # Set by Shopify once the draft is paid, and null until then. The only honest
    # evidence that money arrived.
    order_number: Optional[str] = None
    order_id: Optional[str] = None
    items: List[LineItem] = field(default_factory=list)
    created_at: Optional[str] = None

    @property
    def paid(self) -> bool:
        """Whether this draft has become a real, paid order."""
        return bool(self.order_number)

    @property
    def piece_count(self) -> int:
        return sum(item.quantity for item in self.items)

    def to_tool_dict(self) -> Dict[str, Any]:
        """What the model may see. The Shopify id and the tags never leave this module.

        ``checkout_url`` is the one field that exists to be repeated to the customer;
        everything else is here so the bot can read the basket back to them.
        """
        return {
            "checkout_url": self.checkout_url,
            # Named for what it is. A draft is priced before the customer has chosen a
            # delivery address, so it carries the goods only - calling this "total"
            # invited the bot to read it out as the amount they would pay.
            "items_total": self.total,
            "currency": self.currency,
            "delivery_charged_at_checkout": True,
            "paid": self.paid,
            "items": [item.to_tool_dict() for item in self.items],
        }
