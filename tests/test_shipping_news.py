"""Telling a customer their order has shipped.

Same shape as the feedback trigger, and the same limitation: the bot cannot start a
conversation, so the news rides on the customer's next message. What is asserted here is
that it is said once, only when true, and that it says no more than Shopify actually
knows - "shipped" means the parcel left the shop, not where it is now.
"""

import pytest

from app.modules.chat import agent
from app.modules.orders import repository as orders_repository
from app.modules.orders import service as orders_service
from app.modules.orders.schemas import LineItem, Order, Tracking

CID = "c1"


def _order(**overrides) -> Order:
    fields = dict(id="gid://shopify/Order/555", number="#1011",
                  financial_status="PENDING", fulfillment_status="FULFILLED",
                  cash_on_delivery=True, total="667", currency="EGP",
                  items=[LineItem(title="WANAS CREWNECK", quantity=1,
                                  variant_title="M / Navy")])
    fields.update(overrides)
    return Order(**fields)


def _linked(monkeypatch, order):
    orders_repository.link(CID, "#1011", "web", item_count=1)

    async def staff(_number):
        return order

    monkeypatch.setattr(orders_service, "lookup_for_staff", staff)


# --- when it fires ---------------------------------------------------------


async def test_a_shipped_order_is_news(monkeypatch):
    _linked(monkeypatch, _order())

    news = await orders_service.shipping_news(CID)
    assert [o.number for o in news] == ["#1011"]


async def test_an_unshipped_order_is_not(monkeypatch):
    _linked(monkeypatch, _order(fulfillment_status="UNFULFILLED"))

    assert await orders_service.shipping_news(CID) == []


async def test_a_partly_shipped_order_counts(monkeypatch):
    _linked(monkeypatch, _order(fulfillment_status="PARTIALLY_FULFILLED"))

    assert len(await orders_service.shipping_news(CID)) == 1


async def test_a_cancelled_order_is_never_announced(monkeypatch):
    _linked(monkeypatch, _order(cancelled_at="2026-08-19T10:00:00Z"))

    assert await orders_service.shipping_news(CID) == []


async def test_an_order_already_in_their_hands_is_not_announced(monkeypatch):
    """Telling someone it is on its way while they hold it is worse than silence."""
    _linked(monkeypatch, _order(financial_status="PAID"))

    assert await orders_service.shipping_news(CID) == []


async def test_stale_news_is_not_re_checked_forever(monkeypatch):
    """An arrived order is marked told, so it stops costing a lookup every turn."""
    _linked(monkeypatch, _order(financial_status="PAID"))

    await orders_service.shipping_news(CID)
    assert orders_repository.orders_not_yet_told_shipped(CID) == []


async def test_a_conversation_that_ordered_nothing_has_no_news():
    assert await orders_service.shipping_news("never-ordered") == []


async def test_shopify_being_down_costs_the_news_not_the_chat(monkeypatch):
    orders_repository.link(CID, "#1011", "web")

    async def explode(_number):
        raise RuntimeError("shopify is unreachable")

    monkeypatch.setattr(orders_service, "lookup_for_staff", explode)
    assert await orders_service.shipping_news(CID) == []


# --- said once -------------------------------------------------------------


async def test_it_is_not_repeated_once_they_have_been_told(monkeypatch):
    _linked(monkeypatch, _order())
    assert len(await orders_service.shipping_news(CID)) == 1

    orders_service.mark_shipping_announced(CID, "#1011")

    assert await orders_service.shipping_news(CID) == []


def test_marking_an_order_that_was_never_linked_is_harmless():
    orders_service.mark_shipping_announced(CID, "#9999")
    orders_service.mark_shipping_announced("", "#1011")


# --- what the model is told ------------------------------------------------


def test_the_prompt_names_the_order():
    prompt = agent.build_system_prompt(shipped_orders=[_order()])

    assert "THIS CUSTOMER'S ORDER HAS SHIPPED" in prompt
    assert "#1011" in prompt


def test_tracking_is_included_when_there_is_some():
    order = _order(tracking=Tracking(company="Bosta", number="BST123"))

    prompt = agent.build_system_prompt(shipped_orders=[order])

    assert "Bosta BST123" in prompt


def test_no_tracking_line_when_there_is_none():
    prompt = agent.build_system_prompt(shipped_orders=[_order()])

    assert "Tracking:" not in prompt


def test_the_bot_is_told_not_to_embroider_it():
    """Shopify knows the parcel left. It does not know where it is or when it lands."""
    prompt = agent.build_system_prompt(shipped_orders=[_order()])

    assert "Do not say where the parcel is now" in prompt
    assert "Do not repeat this in later messages" in prompt


def test_nothing_is_said_when_there_is_no_news():
    prompt = agent.build_system_prompt()

    assert "THIS CUSTOMER'S ORDER HAS SHIPPED" not in prompt


def test_shipped_and_arrived_can_both_appear():
    """Different orders in one conversation can be at different stages."""
    prompt = agent.build_system_prompt(arrived_order=_order(number="#1008"),
                                       shipped_orders=[_order(number="#1011")])

    assert "HAS SHIPPED" in prompt and "HAS ARRIVED" in prompt
