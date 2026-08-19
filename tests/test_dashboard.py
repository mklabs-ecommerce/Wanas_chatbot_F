"""The owner-facing dashboard.

Everything behind it is customer personal data - real names, phones, delivery addresses
and whole conversations - so most of what is asserted here is about who gets in.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.modules.chat import repository as chat_repository
from app.modules.dashboard import service as dashboard_service
from app.modules.orders import repository as orders_repository

TOKEN = "test-token-long-enough-to-count"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_token", TOKEN, raising=False)
    return TestClient(app)


@pytest.fixture
def off(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_token", "", raising=False)
    return TestClient(app)


# --- who gets in -----------------------------------------------------------


def test_the_dashboard_does_not_exist_without_a_token_configured(off):
    """Fails closed: an unset variable must not publish customer data."""
    for path in ("/dashboard", "/dashboard/api/conversations"):
        assert off.get(path).status_code == 404, path


def test_an_unconfigured_dashboard_does_not_advertise_itself(off):
    """404 rather than 401 - no hint that there is something here to attack."""
    assert off.get("/dashboard?token=anything").status_code == 404


def test_a_short_token_counts_as_no_token(monkeypatch):
    """A three-character token is a rounding error away from none at all."""
    monkeypatch.setattr(settings, "dashboard_token", "abc", raising=False)
    assert TestClient(app).get("/dashboard?token=abc").status_code == 404


def test_no_token_is_refused(client):
    assert client.get("/dashboard").status_code == 401
    assert client.get("/dashboard/api/conversations").status_code == 401


def test_a_wrong_token_is_refused(client):
    assert client.get("/dashboard?token=wrong").status_code == 401
    assert client.get("/dashboard/api/conversations",
                      headers={"X-Dashboard-Token": "wrong"}).status_code == 401


def test_a_near_miss_token_is_refused(client):
    """Prefix of the real token - the compare is whole-value, not startswith."""
    assert client.get("/dashboard?token=" + TOKEN[:-1]).status_code == 401


def test_the_right_token_works_in_the_query_or_the_header(client):
    assert client.get("/dashboard?token=" + TOKEN).status_code == 200
    assert client.get("/dashboard/api/conversations",
                      headers={"X-Dashboard-Token": TOKEN}).status_code == 200


def test_every_route_is_gated(client):
    """Including the detail route - a leak there is the whole transcript."""
    assert client.get("/dashboard/api/conversations/anything").status_code == 401


def test_health_reports_whether_the_dashboard_is_on(client):
    assert client.get("/health").json()["dashboard_enabled"] is True


# --- what it shows ---------------------------------------------------------


def test_an_empty_install_shows_no_conversations(client):
    body = client.get("/dashboard/api/conversations?token=" + TOKEN).json()
    assert body == {"count": 0, "sort": "recent", "conversations": []}


def test_a_conversation_appears_with_its_message_count(client):
    cid = chat_repository.ensure_conversation(None, channel="web")
    chat_repository.add_message(cid, chat_repository.ROLE_USER, "عندكم هودي؟")
    chat_repository.add_message(cid, chat_repository.ROLE_MODEL, "أيوه عندنا")

    body = client.get("/dashboard/api/conversations?token=" + TOKEN).json()
    assert body["count"] == 1
    row = body["conversations"][0]
    assert row["conversation_id"] == cid
    assert row["message_count"] == 2
    assert row["last_message"] == "أيوه عندنا"
    assert row["order_count"] == 0


def test_orders_placed_in_a_conversation_are_counted(client):
    cid = chat_repository.ensure_conversation(None, channel="web")
    chat_repository.add_message(cid, chat_repository.ROLE_USER, "hi")
    orders_repository.link(cid, "#1008", "web")

    row = client.get("/dashboard/api/conversations?token=" + TOKEN).json()["conversations"][0]
    assert row["order_count"] == 1


def test_the_list_view_never_calls_shopify(client, monkeypatch):
    """It must stay fast however many conversations there are."""
    async def explode(*_a, **_k):
        raise AssertionError("the list view called Shopify")

    monkeypatch.setattr(dashboard_service.orders_service, "lookup_for_staff", explode)
    cid = chat_repository.ensure_conversation(None, channel="web")
    chat_repository.add_message(cid, chat_repository.ROLE_USER, "hi")
    orders_repository.link(cid, "#1008", "web")

    assert client.get("/dashboard/api/conversations?token=" + TOKEN).status_code == 200


async def test_shopify_being_down_costs_the_orders_not_the_page(monkeypatch):
    """A dashboard is a convenience; it must degrade rather than fail."""
    async def explode(_conversation_id):
        raise RuntimeError("shopify is unreachable")

    monkeypatch.setattr(dashboard_service.orders_service, "orders_for_conversation", explode)
    cid = chat_repository.ensure_conversation(None, channel="web")
    chat_repository.add_message(cid, chat_repository.ROLE_USER, "hi")

    detail = await dashboard_service.conversation(cid)
    assert detail["orders"] == []
    assert detail["orders_readable"] is False
    # The part that does not depend on Shopify still arrived.
    assert len(detail["messages"]) == 1


async def test_no_orders_and_unreadable_orders_are_different_facts():
    cid = chat_repository.ensure_conversation(None, channel="web")
    detail = await dashboard_service.conversation(cid)

    assert detail["orders"] == []
    assert detail["orders_readable"] is True


# --- the link between a conversation and its orders ------------------------


def test_linking_an_order_to_a_conversation_is_idempotent():
    orders_repository.link("c1", "#1008", "web")
    orders_repository.link("c1", "#1008", "web")

    assert orders_repository.order_numbers_for("c1") == ["#1008"]
    assert orders_repository.count() == 1


def test_an_order_knows_which_conversation_placed_it():
    orders_repository.link("c1", "#1008", "web")
    assert orders_repository.conversation_for("#1008") == "c1"
    assert orders_repository.conversation_for("#9999") is None


def test_a_missing_conversation_or_order_is_not_linked():
    orders_repository.link("", "#1008", "web")
    orders_repository.link("c1", "", "web")
    assert orders_repository.count() == 0


# --- ordering the list -----------------------------------------------------


def _conversation(messages: int, pieces: int = 0, tickets: int = 0):
    from app.modules.support import repository as support_repository
    from app.modules.support.schemas import Ticket

    cid = chat_repository.ensure_conversation(None, channel="web")
    for n in range(messages):
        chat_repository.add_message(cid, chat_repository.ROLE_USER, "m" + str(n))
    if pieces:
        orders_repository.link(cid, "#" + str(1000 + pieces) + cid[:4], "web",
                               item_count=pieces)
    for n in range(tickets):
        support_repository.create(Ticket(reference="", category="complaint",
                                         summary="x" * 20, contact="a@b.com",
                                         conversation_id=cid))
    return cid


def _order_of(client, sort):
    body = client.get("/dashboard/api/conversations?token=" + TOKEN + "&sort=" + sort).json()
    return body["sort"], [c["conversation_id"] for c in body["conversations"]]


def test_newest_first_is_the_default(client):
    first = _conversation(messages=1)
    second = _conversation(messages=1)

    body = client.get("/dashboard/api/conversations?token=" + TOKEN).json()
    assert body["sort"] == "recent"
    assert [c["conversation_id"] for c in body["conversations"]] == [second, first]


def test_oldest_first_reverses_it(client):
    first = _conversation(messages=1)
    second = _conversation(messages=1)

    _, order = _order_of(client, "oldest")
    assert order == [first, second]


def test_sorting_by_messages(client):
    quiet = _conversation(messages=1)
    busy = _conversation(messages=5)

    _, order = _order_of(client, "messages")
    assert order == [busy, quiet]


def test_sorting_by_pieces_ordered(client):
    none = _conversation(messages=1)
    small = _conversation(messages=1, pieces=1)
    big = _conversation(messages=1, pieces=4)

    _, order = _order_of(client, "pieces")
    assert order[0] == big and order[1] == small and order[2] == none


def test_sorting_by_tickets(client):
    quiet = _conversation(messages=1)
    complained = _conversation(messages=1, tickets=2)

    _, order = _order_of(client, "tickets")
    assert order == [complained, quiet]


def test_an_unknown_sort_falls_back_rather_than_erroring(client):
    """A stale bookmark should still show the page."""
    _conversation(messages=1)

    sort, order = _order_of(client, "by-vibes")
    assert sort == "recent"
    assert len(order) == 1


def test_the_piece_count_comes_from_the_local_record_not_shopify(client, monkeypatch):
    """Sorting by pieces must not cost one API call per row."""
    async def explode(*_a, **_k):
        raise AssertionError("sorting by pieces called Shopify")

    monkeypatch.setattr(dashboard_service.orders_service, "lookup_for_staff", explode)
    _conversation(messages=1, pieces=3)

    body = client.get("/dashboard/api/conversations?token=" + TOKEN + "&sort=pieces").json()
    assert body["conversations"][0]["piece_count"] == 3
