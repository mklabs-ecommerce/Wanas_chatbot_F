"""Data shapes the feedback module exposes."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

# How a piece of feedback reads. The store owner chose (2026-08-19) not to ask customers
# for a score - they just say what they think, in their own words - so this is derived
# from what they wrote rather than requested from them.
SENTIMENTS = ("positive", "neutral", "negative")

# What each reads as in an email subject line.
SENTIMENT_LABELS = {
    "positive": "Happy customer",
    "neutral": "Feedback",
    "negative": "Unhappy customer",
}

# Anything unrecognised lands here. Deliberately the one that reaches a human: an
# unreadable sentiment on a real complaint must not be silently filed as fine.
DEFAULT_SENTIMENT = "negative"


@dataclass
class Feedback:
    """One customer's opinion, in their own words."""

    comment: str
    sentiment: str = DEFAULT_SENTIMENT
    customer_name: str = ""
    contact: str = ""
    order_number: Optional[str] = None
    conversation_id: str = ""
    channel: str = "web"
    created_at: Optional[datetime] = None

    @property
    def label(self) -> str:
        return SENTIMENT_LABELS.get(self.sentiment, SENTIMENT_LABELS[DEFAULT_SENTIMENT])

    @property
    def is_negative(self) -> bool:
        """Whether this is the kind of feedback that should reach a person today."""
        return self.sentiment == "negative"

    def to_tool_dict(self) -> Dict[str, Any]:
        """What the bot may tell the customer back.

        Only that it was written down. There is no reference here on purpose - unlike a
        support ticket, feedback is not something the customer chases up later, and
        handing them a code invites them to expect a reply that is not coming.
        """
        return {"recorded": True, "sentiment": self.sentiment}
