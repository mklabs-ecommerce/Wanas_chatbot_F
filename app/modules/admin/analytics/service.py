"""Assembling the owner dashboard's KPIs from what the other modules already expose.

This module reads and aggregates; it decides nothing about any single order, ticket or
conversation. Every fact comes from another module's public ``service.py`` - there is no
SQL and no Shopify call in this file. If a KPI needs something that function does not
return yet, the fix is to extend that module's service (as ``orders.orders_in_range``,
``support.tickets_in_range``, ``feedback.feedback_in_range`` and
``chat.conversation_count_in_range`` were, for this build), never to reach around it.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from app.core.config import settings
from app.modules.admin.analytics.schemas import PLACEHOLDER_CHANNELS, Snapshot, TopProduct, Trend
from app.modules.chat import service as chat_service
from app.modules.engagement import service as engagement_service
from app.modules.feedback import service as feedback_service
from app.modules.orders import service as orders_service
from app.modules.orders.schemas import Order
from app.modules.support import service as support_service

logger = logging.getLogger(__name__)

TOP_PRODUCTS_LIMIT = 5

# Comments only exist on Instagram - see engagement.service. Computing them for any
# other channel would either be a Shopify-shaped question with no answer, or (for "all")
# double-count nothing since no other channel has comments to contribute.
_CHANNELS_WITH_COMMENTS = (None, "instagram")


def _day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def default_range(today: Optional[date] = None) -> Tuple[date, date]:
    """"This month" (Section 6's default view): the 1st through today.

    ``end`` is exclusive and one day past ``today`` so today itself is fully included -
    every range this module hands out is a half-open ``[start, end)``.
    """
    today = today or datetime.now(timezone.utc).date()
    return today.replace(day=1), today + timedelta(days=1)


def _previous_period(start: date, end: date) -> Tuple[date, date]:
    """The immediately preceding period of the same length, for the trend comparison."""
    length = end - start
    return start - length, start


async def snapshot(channel: Optional[str], start: date, end: date) -> Snapshot:
    """Every core KPI for one channel and date range.

    ``channel=None`` means every channel combined - the "All" tab. A channel in
    ``PLACEHOLDER_CHANNELS`` never touches Shopify or the database: nothing is connected
    there yet, so there is nothing to compute.
    """
    if channel in PLACEHOLDER_CHANNELS:
        return Snapshot(channel=channel, period_start=start, period_end=end, connected=False)

    prev_start, prev_end = _previous_period(start, end)
    start_dt, end_dt = _day_start(start), _day_start(end)
    prev_start_dt, prev_end_dt = _day_start(prev_start), _day_start(prev_end)

    orders, prev_orders = await asyncio.gather(
        orders_service.orders_in_range(start_dt, end_dt, channel),
        orders_service.orders_in_range(prev_start_dt, prev_end_dt, channel),
    )

    tickets = support_service.tickets_in_range(start_dt, end_dt, channel)
    feedback = feedback_service.feedback_in_range(start_dt, end_dt, channel)
    conversation_count = chat_service.conversation_count_in_range(start_dt, end_dt, channel)
    message_count = chat_service.customer_message_count_in_range(start_dt, end_dt, channel)
    timestamps = chat_service.inbound_timestamps_in_range(start_dt, end_dt, channel)

    funnel_comments = funnel_dms_opened = None
    if channel in _CHANNELS_WITH_COMMENTS:
        funnel_comments = engagement_service.comments_received_in_range(start_dt, end_dt)
        funnel_dms_opened = engagement_service.dms_opened_from_comments_in_range(start_dt, end_dt)

    # Cancelled orders never happened commercially - counting their revenue would
    # overstate the shop's income for an order nobody paid for.
    live = [order for order in orders if not order.is_cancelled]
    prev_live = [order for order in prev_orders if not order.is_cancelled]

    revenue = sum(_amount(order) for order in live)
    prev_revenue = sum(_amount(order) for order in prev_live)
    currency = next((order.currency for order in live if order.currency), "EGP")

    cod = [order for order in live if order.payment_method == "cash_on_delivery"]
    online = [order for order in live if order.payment_method == "online"]

    hours, weekdays = _activity_buckets(timestamps)

    return Snapshot(
        channel=channel,
        period_start=start,
        period_end=end,
        order_count=Trend(len(live), len(prev_live)),
        revenue=Trend(revenue, prev_revenue),
        average_order_value=(revenue / len(live)) if live else 0.0,
        currency=currency,
        top_products=_top_products(live),
        daily=_daily_series(live, start, end),
        new_customers=sum(1 for order in live if order.is_new_customer is True),
        returning_customers=sum(1 for order in live if order.is_new_customer is False),
        conversation_count=conversation_count,
        message_count=message_count,
        ticket_count=len(tickets),
        tickets_by_status=_count_by(ticket.status for ticket in tickets),
        feedback_count=len(feedback),
        feedback_by_sentiment=_count_by(item.sentiment for item in feedback),
        cod_order_count=len(cod),
        cod_revenue=sum(_amount(order) for order in cod),
        online_order_count=len(online),
        online_revenue=sum(_amount(order) for order in online),
        escalation_rate_pct=_pct(len(tickets), conversation_count),
        funnel_comments=funnel_comments,
        funnel_dms_opened=funnel_dms_opened,
        funnel_comment_to_dm_pct=_pct(funnel_dms_opened, funnel_comments),
        funnel_conversation_to_order_pct=_pct(len(live), conversation_count),
        busiest_hours=hours,
        busiest_days=weekdays,
        utc_offset_hours=settings.store_utc_offset_hours,
    )


def _amount(order: Order) -> float:
    try:
        return float(order.total or 0)
    except (TypeError, ValueError):
        return 0.0


def _top_products(orders: List[Order], limit: int = TOP_PRODUCTS_LIMIT) -> List[TopProduct]:
    """Ranked by pieces sold. Revenue is split evenly across an order's items to give
    each product a figure - ``Order`` carries only one total, never a per-line price."""
    by_title: Dict[str, TopProduct] = {}
    for order in orders:
        if not order.items:
            continue
        pieces = sum(item.quantity for item in order.items) or 1
        per_piece = _amount(order) / pieces
        for item in order.items:
            row = by_title.setdefault(item.title, TopProduct(item.title, 0, 0.0))
            row.quantity += item.quantity
            row.revenue += per_piece * item.quantity
    return sorted(by_title.values(), key=lambda product: product.quantity, reverse=True)[:limit]


def _daily_series(orders: List[Order], start: date, end: date) -> List[Dict[str, object]]:
    """One row per calendar day in ``[start, end)``, even a day with no orders.

    A bar chart needs every day present, or a quiet day silently vanishes from the
    x-axis instead of showing as zero - a different fact from "no data for that day".
    Capped: a date range spanning years would build a day per iteration, so this is
    only sound for the ranges the dashboard actually offers (day/week/month/year).
    """
    buckets: Dict[str, Dict[str, float]] = {}
    day = start
    while day < end:
        buckets[day.isoformat()] = {"orders": 0, "revenue": 0.0}
        day += timedelta(days=1)

    for order in orders:
        bucket = buckets.get(order.placed_on)
        if bucket is not None:
            bucket["orders"] += 1
            bucket["revenue"] += _amount(order)

    return [{"date": day, "orders": int(values["orders"]), "revenue": round(values["revenue"], 2)}
            for day, values in buckets.items()]


def _count_by(values) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _pct(numerator: Optional[int], denominator: Optional[int]) -> Optional[float]:
    """``None`` rather than a division by zero, and rather than a rate over nothing -
    an empty denominator means there is nothing to report, not a 0% rate."""
    if not numerator and numerator != 0:
        return None
    if not denominator:
        return None
    return round(numerator / denominator * 100, 1)


def _activity_buckets(timestamps: List[datetime]) -> Tuple[List[int], List[int]]:
    """Customer message timestamps, bucketed by store-local hour and weekday.

    ``timestamps`` come back from SQLite with no tzinfo; they were always stored as
    UTC (see ``chat/repository.py``), so a naive value is treated as UTC before the
    store's offset is applied - the same fix the admin session-expiry check needed.
    """
    hours = [0] * 24
    weekdays = [0] * 7
    offset = timedelta(hours=settings.store_utc_offset_hours)
    for value in timestamps:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        local = value.astimezone(timezone.utc) + offset
        hours[local.hour] += 1
        weekdays[local.weekday()] += 1
    return hours, weekdays
