"""Data shapes the support module exposes."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

# What a ticket is about. Kept short and closed so the store owner can sort a mailbox by
# it, and so the model picks from a list rather than inventing a taxonomy of its own.
CATEGORIES = (
    "damaged_or_faulty",
    "wrong_item",
    "delivery_problem",
    "return_or_exchange",
    "change_or_cancel_order",
    "payment_problem",
    "complaint",
    "other",
)

# How each reads in an email subject line.
CATEGORY_LABELS = {
    "damaged_or_faulty": "Damaged or faulty item",
    "wrong_item": "Wrong item received",
    "delivery_problem": "Delivery problem",
    "return_or_exchange": "Return or exchange",
    "change_or_cancel_order": "Change or cancel an order",
    "payment_problem": "Payment problem",
    "complaint": "Complaint",
    "other": "Other",
}

STATUS_OPEN = "open"


@dataclass
class Ticket:
    """One issue logged for a human to pick up."""

    reference: str
    category: str
    summary: str
    customer_name: str = ""
    contact: str = ""
    order_number: Optional[str] = None
    conversation_id: str = ""
    channel: str = "web"
    status: str = STATUS_OPEN
    created_at: Optional[datetime] = None

    @property
    def label(self) -> str:
        return CATEGORY_LABELS.get(self.category, CATEGORY_LABELS["other"])

    def to_tool_dict(self) -> Dict[str, Any]:
        """What the bot may tell the customer back.

        Only the reference and what it was logged as - the customer already knows what
        they said, and reading their own contact details back adds nothing.
        """
        return {
            "reference": self.reference,
            "category": self.category,
            "status": self.status,
        }
