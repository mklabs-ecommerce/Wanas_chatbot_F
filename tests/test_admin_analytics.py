"""admin.analytics: KPI aggregation for the owner dashboard, Web channel only (step 2).

Order data is faked at the Shopify client boundary, same as tests/test_orders.py.
Tickets, feedback and conversations go through their real modules against the test
database, so this also exercises the new orders.orders_in_range /
support.tickets_in_range / feedback.feedback_in_range / chat.conversation_count_in_range
functions added for this module to call.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.admin.analytics import service as analytics_service
from app.modules.admin.auth import service as auth_service
from app.modules.admin.auth.schemas import OWNER
from app.modules.chat import repository as chat_repository
from app.modules.engagement import repository as engagement_repository
from app.modules.feedback import service as feedback_service
from app.modules.notifications import service as notifications
from app.modules.orders import service as orders_service
from app.modules.support import service as support_service


def _order(number, total="500.00", tags=None, created_at=None, quantity=1,
          title="BOXY WNS TEE", customer_orders="1", cancelled_at=None):
    return {
        "id": "gid://shopify/Order/%s" % number,
        "name": "#%s" % number,
        "createdAt": created_at or "2026-08-10T12:00:00Z",
        "cancelledAt": cancelled_at,
        "cancelReason": None,
        "displayFinancialStatus": "PENDING",
        "displayFulfillmentStatus": "UNFULFILLED",
        "email": None,
        "phone": "+201067177129",
        "tags": tags if tags is not None else ["cash-on-delivery", "chatbot", "web"],
        "totalPriceSet": {"shopMoney": {"amount": total, "currencyCode": "EGP"}},
        "currentTotalPriceSet": {"shopMoney": {"amount": total, "currencyCode": "EGP"}},
        "subtotalPriceSet": {"shopMoney": {"amount": total, "currencyCode": "EGP"}},
        "totalShippingPriceSet": {"shopMoney": {"amount": "0", "currencyCode": "EGP"}},
        "shippingLine": {"title": ""},
        "customer": {"email": None, "phone": "+201067177129",
                    "numberOfOrders": customer_orders},
        "shippingAddress": {"name": "Mona", "phone": "+201067177129",
                           "city": "Cairo", "province": "Cairo", "country": "Egypt"},
        "lineItems": {"nodes": [
            {"title": title, "quantity": quantity, "variantTitle": "M / Black", "sku": "x"},
        ]},
        "fulfillments": [],
    }


class FakeShopify:
    def __init__(self, nodes):
        self.nodes = nodes
        self.queries = []

    async def fetch_all_orders(self, query=None, page_size=100, max_pages=50):
        self.queries.append(query)
        return self.nodes


@pytest.fixture
def orders(monkeypatch):
    fake = FakeShopify([
        _order("2001", total="500.00", tags=["cash-on-delivery", "chatbot", "web"],
              customer_orders="1"),
        _order("2002", total="300.00", tags=["online-payment", "chatbot", "web"],
              title="BASIC TEE", customer_orders="3"),
        # A different channel - must not appear in the "web" snapshot.
        _order("2003", total="900.00", tags=["cash-on-delivery", "chatbot", "instagram"]),
        # Cancelled - must not count towards revenue or order count.
        _order("2004", total="200.00", tags=["cash-on-delivery", "chatbot", "web"],
              cancelled_at="2026-08-11T00:00:00Z"),
        # No chatbot tag at all (a manual/POS order) - orders_in_range filters these out
        # at the query level, so it is never handed to _to_order here either.
    ])
    monkeypatch.setattr(orders_service, "_shopify", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def quiet_notifications(monkeypatch):
    async def ok(*args, **kwargs):
        return True
    monkeypatch.setattr(notifications, "notify_new_ticket", ok)
    monkeypatch.setattr(support_service.notifications, "notify_new_ticket", ok)
    monkeypatch.setattr(notifications, "notify_new_feedback", ok)
    monkeypatch.setattr(feedback_service.notifications, "notify_new_feedback", ok)


THIS_MONTH = date(2026, 8, 1), date(2026, 8, 31)


# --- date ranges -------------------------------------------------------------


def test_default_range_is_the_1st_through_today():
    start, end = analytics_service.default_range(today=date(2026, 8, 20))
    assert start == date(2026, 8, 1)
    assert end == date(2026, 8, 21)  # exclusive, so the 20th is fully included


def test_previous_period_is_the_same_length_immediately_before():
    prev_start, prev_end = analytics_service._previous_period(date(2026, 8, 1), date(2026, 8, 21))
    assert prev_end == date(2026, 8, 1)
    assert (prev_end - prev_start) == timedelta(days=20)


# --- orders ------------------------------------------------------------------


async def test_only_chatbot_tagged_orders_for_the_right_channel_are_counted(orders):
    start, end = THIS_MONTH
    snapshot = await analytics_service.snapshot("web", start, end)
    assert snapshot.order_count.current == 2  # 2001, 2002 - not 2003 (instagram) or 2004 (cancelled)
    assert snapshot.revenue.current == 800.0
    assert snapshot.currency == "EGP"


async def test_a_query_asks_shopify_for_the_channel_and_the_chatbot_tag(orders):
    await analytics_service.snapshot("web", *THIS_MONTH)
    assert "tag:'chatbot'" in orders.queries[0]
    assert "tag:'web'" in orders.queries[0]


async def test_average_order_value(orders):
    snapshot = await analytics_service.snapshot("web", *THIS_MONTH)
    assert snapshot.average_order_value == 400.0  # (500 + 300) / 2


async def test_daily_series_has_one_row_per_day_including_zero_days(orders):
    snapshot = await analytics_service.snapshot("web", date(2026, 8, 9), date(2026, 8, 12))
    by_date = {row["date"]: row for row in snapshot.daily}
    assert set(by_date) == {"2026-08-09", "2026-08-10", "2026-08-11"}
    assert by_date["2026-08-10"]["orders"] == 2  # 2001 and 2002, both dated 2026-08-10
    assert by_date["2026-08-10"]["revenue"] == 800.0
    assert by_date["2026-08-09"] == {"date": "2026-08-09", "orders": 0, "revenue": 0.0}


async def test_top_products_ranked_by_quantity(orders):
    snapshot = await analytics_service.snapshot("web", *THIS_MONTH)
    titles = [p.title for p in snapshot.top_products]
    assert titles[:2] == ["BOXY WNS TEE", "BASIC TEE"]


async def test_new_vs_returning_customers(orders):
    snapshot = await analytics_service.snapshot("web", *THIS_MONTH)
    assert snapshot.new_customers == 1  # 2001, numberOfOrders="1"
    assert snapshot.returning_customers == 1  # 2002, numberOfOrders="3"


async def test_cod_vs_online_split(orders):
    snapshot = await analytics_service.snapshot("web", *THIS_MONTH)
    assert snapshot.cod_order_count == 1 and snapshot.cod_revenue == 500.0
    assert snapshot.online_order_count == 1 and snapshot.online_revenue == 300.0


async def test_a_cancelled_order_counts_nowhere(orders):
    snapshot = await analytics_service.snapshot("web", *THIS_MONTH)
    assert snapshot.order_count.current == 2
    assert snapshot.revenue.current == 800.0


async def test_the_trend_compares_against_the_immediately_preceding_period(monkeypatch):
    fake = FakeShopify([])
    calls = []

    async def recording_fetch(query=None, page_size=100, max_pages=50):
        calls.append(query)
        if ">='2026-08-11'" in query:
            return [_order("3001", total="100.00", created_at="2026-08-15T12:00:00Z")]
        return [_order("3002", total="40.00", created_at="2026-08-05T12:00:00Z"),
                _order("3003", total="60.00", created_at="2026-08-06T12:00:00Z")]

    fake.fetch_all_orders = recording_fetch
    monkeypatch.setattr(orders_service, "_shopify", lambda: fake)

    snapshot = await analytics_service.snapshot("web", date(2026, 8, 11), date(2026, 8, 21))
    assert snapshot.revenue.current == 100.0
    assert snapshot.revenue.previous == 100.0
    assert snapshot.revenue.to_dict()["change_pct"] == 0.0


def test_change_pct_is_none_rather_than_a_division_by_zero():
    from app.modules.admin.analytics.schemas import Trend
    assert Trend(current=50.0, previous=0.0).change_pct is None
    assert Trend(current=0.0, previous=0.0).change_pct is None


# --- placeholder channels ----------------------------------------------------


@pytest.mark.parametrize("channel", ["whatsapp", "tiktok", "facebook"])
async def test_an_unconnected_channel_never_touches_shopify_or_the_database(channel, monkeypatch):
    def boom():
        raise AssertionError("an unconnected channel must not call Shopify")
    monkeypatch.setattr(orders_service, "_shopify", boom)

    snapshot = await analytics_service.snapshot(channel, *THIS_MONTH)
    assert snapshot.connected is False
    assert snapshot.order_count.current == 0


# --- tickets, feedback, conversations (real DB, no Shopify involved) --------


async def test_ticket_volume_and_status_breakdown(orders):
    await support_service.create_ticket(
        category="damaged_or_faulty", summary="The sleeve seam has come apart at the shoulder.",
        contact="mona@example.com", channel="web")
    await support_service.create_ticket(
        category="delivery_problem", summary="It has been eight days and nothing has arrived.",
        contact="sara@example.com", channel="instagram")  # different channel

    today = datetime.now(timezone.utc).date()
    snapshot = await analytics_service.snapshot("web", today, today + timedelta(days=1))
    assert snapshot.ticket_count == 1
    assert snapshot.tickets_by_status == {"open": 1}
    # No resolution workflow exists yet - the field says so rather than faking a number.
    assert snapshot.to_dict()["support_tickets"]["resolution_tracking_available"] is False


async def test_feedback_volume_and_sentiment_breakdown(orders):
    await feedback_service.record_feedback(comment="حلو جدا", sentiment="positive",
                                           conversation_id="c1", channel="web")
    await feedback_service.record_feedback(comment="مش عاجبني", sentiment="negative",
                                           conversation_id="c2", channel="web")
    await feedback_service.record_feedback(comment="okay", sentiment="positive",
                                           conversation_id="c3", channel="instagram")

    today = datetime.now(timezone.utc).date()
    snapshot = await analytics_service.snapshot("web", today, today + timedelta(days=1))
    assert snapshot.feedback_count == 2
    assert snapshot.feedback_by_sentiment == {"positive": 1, "negative": 1}
    assert snapshot.to_dict()["feedback"]["rating_available"] is False


async def test_conversation_and_message_volume(orders):
    chat_repository.ensure_conversation("conv-a", channel="web")
    chat_repository.add_message("conv-a", "user", "hi")
    chat_repository.add_message("conv-a", "model", "hello")
    chat_repository.ensure_conversation("conv-b", channel="instagram")
    chat_repository.add_message("conv-b", "user", "hi")

    today = datetime.now(timezone.utc).date()
    snapshot = await analytics_service.snapshot("web", today, today + timedelta(days=1))
    assert snapshot.conversation_count == 1
    assert snapshot.message_count == 1  # customer messages only, not the bot's reply


# --- router: auth-gated, every channel (step 4) -----------------------------


@pytest.fixture
def logged_in(orders):
    account = auth_service._create_account("owner1", "a fine password here", OWNER)
    result = auth_service.login("owner1", "a fine password here")
    return {"Authorization": "Bearer " + result.token}


def test_the_analytics_route_requires_a_session(orders):
    client = TestClient(app)
    assert client.get("/admin/api/analytics/web").status_code == 401


def test_a_logged_in_account_gets_the_web_snapshot(logged_in):
    client = TestClient(app)
    response = client.get("/admin/api/analytics/web", headers=logged_in)
    assert response.status_code == 200
    body = response.json()
    assert body["channel"] == "web"
    assert "orders" in body and "revenue" in body


def test_the_instagram_channel_is_now_reachable(logged_in):
    client = TestClient(app)
    response = client.get("/admin/api/analytics/instagram", headers=logged_in)
    assert response.status_code == 200
    assert response.json()["channel"] == "instagram"


def test_the_all_route_aggregates_every_channel(logged_in):
    client = TestClient(app)
    response = client.get("/admin/api/analytics/all", headers=logged_in)
    assert response.status_code == 200
    body = response.json()
    assert body["channel"] == "all"
    # The fake store has 2 web orders (2001, 2002) and 1 instagram order (2003) that
    # count; "all" must not filter any of them out.
    assert body["orders"]["current"] == 3


@pytest.mark.parametrize("channel", ["whatsapp", "tiktok", "facebook"])
def test_a_placeholder_channel_route_says_so_rather_than_faking_zero_data(logged_in, channel):
    client = TestClient(app)
    response = client.get("/admin/api/analytics/%s" % channel, headers=logged_in)
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert body["channel"] == channel


def test_an_unknown_channel_is_still_refused(logged_in):
    client = TestClient(app)
    response = client.get("/admin/api/analytics/snapchat", headers=logged_in)
    assert response.status_code == 400


def test_a_custom_date_range_is_honoured(logged_in, orders):
    client = TestClient(app)
    response = client.get("/admin/api/analytics/web",
                          params={"start": "2026-08-01", "end": "2026-08-31"},
                          headers=logged_in)
    assert response.status_code == 200
    assert response.json()["period"] == {"start": "2026-08-01", "end": "2026-08-31"}


def test_end_before_start_is_rejected(logged_in):
    client = TestClient(app)
    response = client.get("/admin/api/analytics/web",
                          params={"start": "2026-08-10", "end": "2026-08-01"},
                          headers=logged_in)
    assert response.status_code == 400


# --- v1 additional KPIs: escalation rate, funnel, busiest hours (step 3) ----


async def test_escalation_rate_is_tickets_over_conversations(orders):
    chat_repository.ensure_conversation("conv-e1", channel="web")
    chat_repository.ensure_conversation("conv-e2", channel="web")
    chat_repository.ensure_conversation("conv-e3", channel="web")
    await support_service.create_ticket(
        category="payment_problem", summary="The payment link says the basket expired.",
        contact="a@example.com", channel="web", conversation_id="conv-e1")

    today = datetime.now(timezone.utc).date()
    snapshot = await analytics_service.snapshot("web", today, today + timedelta(days=1))
    assert snapshot.conversation_count == 3
    assert snapshot.ticket_count == 1
    assert snapshot.escalation_rate_pct == pytest.approx(33.3, abs=0.1)


async def test_escalation_rate_is_none_with_no_conversations(orders):
    snapshot = await analytics_service.snapshot(
        "web", date(2019, 1, 1), date(2019, 1, 2))  # a date with nothing in it
    assert snapshot.conversation_count == 0
    assert snapshot.escalation_rate_pct is None


async def test_funnel_is_none_for_a_channel_with_no_comments(orders):
    snapshot = await analytics_service.snapshot("web", *THIS_MONTH)
    assert snapshot.funnel_comments is None
    assert snapshot.funnel_dms_opened is None
    assert snapshot.funnel_comment_to_dm_pct is None
    # Orders and conversations still show up - only the comment stage is inapplicable.
    assert snapshot.to_dict()["funnel"]["orders"] == snapshot.order_count.current


async def test_funnel_counts_comments_and_dms_opened_from_them(orders):
    today = datetime.now(timezone.utc)
    engagement_repository.claim("comment-1", engagement_repository.KIND_COMMENT)
    engagement_repository.claim("comment-2", engagement_repository.KIND_COMMENT)
    engagement_repository.claim("comment-3", engagement_repository.KIND_COMMENT)
    engagement_repository.link_thread("igsid-1", "conv-x", opened_from_comment="comment-1")
    # A thread with no comment behind it - a customer who messaged directly - must not
    # count towards a funnel that starts at a comment.
    engagement_repository.link_thread("igsid-2", "conv-y")

    start = (today - timedelta(hours=1)).date()
    end = (today + timedelta(days=1)).date()
    snapshot = await analytics_service.snapshot(None, start, end)  # "All" - includes instagram
    assert snapshot.funnel_comments == 3
    assert snapshot.funnel_dms_opened == 1
    assert snapshot.funnel_comment_to_dm_pct == pytest.approx(33.3, abs=0.1)


async def test_busiest_hours_and_days_count_customer_messages_in_store_local_time(monkeypatch, orders):
    monkeypatch.setattr(analytics_service.settings, "store_utc_offset_hours", 2.0, raising=False)
    # 22:30 UTC on a Monday -> 00:30 local on Tuesday.
    chat_repository.ensure_conversation("conv-hours", channel="web")
    with_time = datetime(2026, 8, 10, 22, 30, tzinfo=timezone.utc)  # a Monday
    import app.modules.chat.repository as repo

    with_session = repo.session_scope
    with with_session() as session:
        session.add(repo.ChatMessage(conversation_id="conv-hours", role="user",
                                     content="hi", created_at=with_time))

    snapshot = await analytics_service.snapshot(
        "web", date(2026, 8, 10), date(2026, 8, 12))
    assert snapshot.busiest_hours[0] == 1  # 00:30 local
    assert snapshot.busiest_days[1] == 1  # Tuesday, weekday index 1
    assert sum(snapshot.busiest_hours) == 1
    assert snapshot.utc_offset_hours == 2.0


async def test_busiest_hours_only_counts_customer_messages_not_replies(orders):
    chat_repository.ensure_conversation("conv-reply", channel="web")
    chat_repository.add_message("conv-reply", "user", "hi")
    chat_repository.add_message("conv-reply", "model", "hello")

    today = datetime.now(timezone.utc).date()
    snapshot = await analytics_service.snapshot("web", today, today + timedelta(days=1))
    assert sum(snapshot.busiest_hours) == 1
