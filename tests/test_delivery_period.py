"""How long delivery takes.

Shopify carries no delivery time - verified 2026-08-19 against the live store, whose one
Domestic zone covers all 29 governorates with a single rate whose description is empty.
So the period is configuration, and the interesting behaviour is what happens when nobody
has set it: the bot must say nothing rather than guess, which is the rule that exists
because it once invented delivery dates.
"""

import pytest

from app.core.config import settings
from app.modules.chat import agent, tools
from app.modules.orders import service as orders_service


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "delivery_days_min", 3, raising=False)
    monkeypatch.setattr(settings, "delivery_days_max", 5, raising=False)
    monkeypatch.setattr(settings, "delivery_working_days", True, raising=False)


@pytest.fixture
def unset(monkeypatch):
    monkeypatch.setattr(settings, "delivery_days_min", 0, raising=False)
    monkeypatch.setattr(settings, "delivery_days_max", 0, raising=False)


def test_the_period_is_reported_when_it_is_configured(configured):
    assert orders_service.delivery_period() == {
        "min_days": 3, "max_days": 5, "working_days": True}


def test_nothing_is_reported_when_nobody_has_said(unset):
    """None, not a guess. The prompt then forbids naming any number of days."""
    assert orders_service.delivery_period() is None


def test_a_nonsense_range_counts_as_unset(monkeypatch):
    monkeypatch.setattr(settings, "delivery_days_min", 5, raising=False)
    monkeypatch.setattr(settings, "delivery_days_max", 2, raising=False)
    assert orders_service.delivery_period() is None


async def test_a_placed_order_carries_the_period(configured, monkeypatch):
    """So the confirmation and the timing come out of one result."""
    from app.modules.orders.schemas import LineItem, Order

    async def created(**_kwargs):
        return Order(id="gid://shopify/Order/1", number="#1011", total="667",
                     currency="EGP", items=[LineItem(title="X", quantity=1)])

    monkeypatch.setattr(orders_service, "create_cod_order", created)

    result = await tools.dispatch(
        "create_cod_order",
        {"items": [{"product": "X", "size": "M", "color": "Navy"}],
         "customer_name": "A B", "phone": "01021255687", "address1": "1 St",
         "city": "Cairo", "governorate": "Cairo", "customer_confirmed": True},
        context=tools.ToolContext(conversation_id="c1"))

    assert result["created"] is True
    assert result["delivery_period"] == {"min_days": 3, "max_days": 5,
                                         "working_days": True}


async def test_a_placed_order_omits_the_period_when_it_is_unset(unset, monkeypatch):
    from app.modules.orders.schemas import LineItem, Order

    async def created(**_kwargs):
        return Order(id="gid://shopify/Order/1", number="#1011", total="667",
                     currency="EGP", items=[LineItem(title="X", quantity=1)])

    monkeypatch.setattr(orders_service, "create_cod_order", created)

    result = await tools.dispatch(
        "create_cod_order",
        {"items": [{"product": "X", "size": "M", "color": "Navy"}],
         "customer_name": "A B", "phone": "01021255687", "address1": "1 St",
         "city": "Cairo", "governorate": "Cairo", "customer_confirmed": True},
        context=tools.ToolContext(conversation_id="c1"))

    assert "delivery_period" not in result


async def test_the_delivery_cost_answer_carries_it_too(configured, monkeypatch):
    """Someone asking about delivery usually wants the price and the wait."""
    async def rate(_governorate, subtotal=0):
        return {"title": "قياسي", "amount": "118.0", "currency": "EGP"}

    monkeypatch.setattr(orders_service, "delivery_cost", rate)

    result = await tools.dispatch("get_delivery_cost", {"governorate": "Cairo"})

    assert result["available"] is True
    assert result["delivery_period"]["min_days"] == 3


def test_the_prompt_allows_a_time_only_from_a_tool():
    prompt = agent.build_system_prompt()

    assert "HOW LONG DELIVERY TAKES" in prompt
    assert "Never name a number of days that did not come from a tool" in prompt
    # Still forbidden to turn a range into a date.
    assert "never a date" in prompt


def test_the_prompt_still_forbids_inventing_a_delivery_date():
    """The original rule survives; the period is a narrow, sourced exception to it."""
    prompt = agent.build_system_prompt()

    assert "Never estimate a delivery date" in prompt
