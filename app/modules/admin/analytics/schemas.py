"""Data shapes admin.analytics exposes - the numbers behind the dashboard's KPI cards.

Every channel tab the dashboard shows (owner-dashboard-plan.md Section 1): the two the
chatbot actually runs on today, ``web`` and ``instagram``, and three that read as
"not connected yet" until their integrations exist - ``tiktok`` per the plan, and
``whatsapp``/``facebook`` because ``CLAUDE.md`` records neither as built. Treating an
unconnected channel as real-but-empty would look identical to a connected channel that
simply had a quiet month, which is worse than saying plainly that it isn't wired up.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

CHANNELS = ("web", "whatsapp", "instagram", "tiktok", "facebook")

# Channels with no integration built at all - Section 1's tabs exist, but there is
# nothing to compute. See modules/engagement (instagram) and modules/chat (web) for the
# two that are real; CLAUDE.md's "out of scope" line is why whatsapp/facebook are not.
PLACEHOLDER_CHANNELS = ("whatsapp", "tiktok", "facebook")


@dataclass
class TopProduct:
    title: str
    quantity: int
    revenue: float

    def to_dict(self) -> Dict[str, Any]:
        return {"title": self.title, "quantity": self.quantity,
                "revenue": round(self.revenue, 2)}


@dataclass
class Trend:
    """A value plus how it compares to the immediately preceding period of equal length."""

    current: float
    previous: float

    @property
    def change_pct(self) -> Optional[float]:
        """``None`` when there is nothing to compare against - not the same as 0%."""
        if not self.previous:
            return None
        return round((self.current - self.previous) / self.previous * 100, 1)

    def to_dict(self) -> Dict[str, Any]:
        return {"current": round(self.current, 2), "previous": round(self.previous, 2),
                "change_pct": self.change_pct}


@dataclass
class Snapshot:
    """Every core KPI (owner-dashboard-plan.md Section 3's first list) for one channel
    and date range. ``channel=None`` means every channel combined - the "All" tab."""

    channel: Optional[str]
    period_start: date
    period_end: date
    # False only for a channel with no integration built yet - see PLACEHOLDER_CHANNELS.
    connected: bool = True

    order_count: Trend = field(default_factory=lambda: Trend(0, 0))
    revenue: Trend = field(default_factory=lambda: Trend(0, 0))
    average_order_value: float = 0.0
    currency: str = "EGP"

    top_products: List[TopProduct] = field(default_factory=list)

    # One row per calendar day in the period, even a day with no orders - for the
    # revenue-over-time bar chart (Section 3). A day missing from the list would read
    # as "no data" rather than "genuinely zero", which is a different fact.
    daily: List[Dict[str, Any]] = field(default_factory=list)

    new_customers: int = 0
    returning_customers: int = 0

    conversation_count: int = 0
    message_count: int = 0

    ticket_count: int = 0
    tickets_by_status: Dict[str, int] = field(default_factory=dict)

    feedback_count: int = 0
    feedback_by_sentiment: Dict[str, int] = field(default_factory=dict)

    cod_order_count: int = 0
    cod_revenue: float = 0.0
    online_order_count: int = 0
    online_revenue: float = 0.0

    # -- v1 additional KPIs (owner-dashboard-plan.md Section 6) --------------

    # How often a conversation on this channel produced a support ticket. Read as "how
    # often a conversation needed a human", not literally "how often the bot failed" -
    # every ticket category counts, including ones that were never the bot's fault (a
    # damaged item, a wrong size sent). ``None`` when there were no conversations to
    # divide by.
    escalation_rate_pct: Optional[float] = None

    # Comments -> DMs opened -> orders. Comments only exist on Instagram, so the first
    # two figures are ``None`` for every other channel rather than a misleading 0 - a
    # web visitor was never going to leave a "comment". Orders reuse ``order_count``.
    funnel_comments: Optional[int] = None
    funnel_dms_opened: Optional[int] = None
    funnel_comment_to_dm_pct: Optional[float] = None
    funnel_conversation_to_order_pct: Optional[float] = None

    # When customers actually message, in the store's local time (see
    # ``store_utc_offset_hours``) - for COD dispatch and staffing decisions. Index 0-23
    # for the hour; index 0=Monday..6=Sunday for the weekday.
    busiest_hours: List[int] = field(default_factory=lambda: [0] * 24)
    busiest_days: List[int] = field(default_factory=lambda: [0] * 7)
    utc_offset_hours: float = 2.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel": self.channel or "all",
            "period": {"start": self.period_start.isoformat(),
                      "end": self.period_end.isoformat()},
            "connected": self.connected,
            "orders": self.order_count.to_dict(),
            "revenue": self.revenue.to_dict(),
            "average_order_value": round(self.average_order_value, 2),
            "currency": self.currency,
            "top_products": [product.to_dict() for product in self.top_products],
            "daily": self.daily,
            "customers": {"new": self.new_customers, "returning": self.returning_customers},
            "conversations": {"count": self.conversation_count, "messages": self.message_count},
            "support_tickets": {
                "count": self.ticket_count,
                "by_status": self.tickets_by_status,
                # Every ticket in this app is "open" forever - nothing transitions a
                # ticket's status yet, so a resolution-rate number would be fake. See
                # modules/support/schemas.py: STATUS_OPEN is the only status that exists.
                "resolution_tracking_available": False,
            },
            "feedback": {
                "count": self.feedback_count,
                "by_sentiment": self.feedback_by_sentiment,
                # The store deliberately never asks for a numeric score (owner's call,
                # 2026-08-19 - see modules/feedback). There is no rating to average.
                "rating_available": False,
            },
            "payment_split": {
                "cash_on_delivery": {"count": self.cod_order_count,
                                    "revenue": round(self.cod_revenue, 2)},
                "online": {"count": self.online_order_count,
                          "revenue": round(self.online_revenue, 2)},
            },
            "escalation": {
                "rate_pct": self.escalation_rate_pct,
                "ticket_count": self.ticket_count,
                "conversation_count": self.conversation_count,
            },
            "funnel": {
                "comments": self.funnel_comments,
                "dms_opened": self.funnel_dms_opened,
                "conversations": self.conversation_count,
                "orders": self.order_count.current,
                "comment_to_dm_pct": self.funnel_comment_to_dm_pct,
                "conversation_to_order_pct": self.funnel_conversation_to_order_pct,
            },
            "activity": {
                "by_hour": self.busiest_hours,
                "by_weekday": self.busiest_days,
                "utc_offset_hours": self.utc_offset_hours,
            },
        }
