"""Cancelling an order.

The rule comes straight from policy.md: cancellation is allowed before the order ships,
and once it has shipped an exchange applies instead. Two guards the policy takes for
granted are asserted here too - the asker must prove the order is theirs, and money that
has already changed hands is a refund decision for a person.

This is the second irreversible thing the bot can do, so most of these tests are about
what stops it.
"""

import pytest

from app.modules.chat import tools
from app.modules.orders import service as orders_service
from app.modules.orders.schemas import LineItem, Order
from app.modules.orders.service import CancelRefused, OrderRejected


def _order(**overrides) -> Order:
    fields = dict(id="gid://shopify/Order/555", number="#1008",
                  financial_status="PENDING", fulfillment_status="UNFULFILLED",
                  cash_on_delivery=True, total="667", currency="EGP",
                  phone="+201021255687", email="mona@example.com",
                  items=[LineItem(title="WANAS CREWNECK", quantity=1,
                                  variant_title="M / Navy")])
    fields.update(overrides)
    return Order(**fields)


class _FakeClient:
    """Records what was asked of Shopify without asking it."""

    def __init__(self, refuse=None):
        self.calls = []
        self.refuse = refuse

    async def cancel_order(self, **kwargs):
        self.calls.append(kwargs)
        if self.refuse:
            raise self.refuse
        return {"id": "gid://shopify/Job/1", "done": False}


@pytest.fixture
def shopify(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(orders_service, "_shopify", lambda: client)
    return client


def _allow(monkeypatch, order):
    """Make get_order_status return this order, and the re-read return it cancelled."""
    async def status(_number, _contact=None):
        return order

    async def staff(_number):
        return _order(cancelled_at="2026-08-19T12:00:00Z",
                      fulfillment_status=order.fulfillment_status)

    monkeypatch.setattr(orders_service, "get_order_status", status)
    monkeypatch.setattr(orders_service, "lookup_for_staff", staff)


# --- what may be cancelled -------------------------------------------------


def test_an_unshipped_unpaid_order_may_be_cancelled():
    assert orders_service.cancellable(_order()) is None


def test_a_shipped_order_may_not_be_cancelled():
    """policy.md: cancellation ends when the parcel leaves the shop."""
    assert orders_service.cancellable(_order(fulfillment_status="FULFILLED")) == "already_shipped"


def test_a_partly_shipped_order_may_not_be_cancelled():
    assert orders_service.cancellable(
        _order(fulfillment_status="PARTIALLY_FULFILLED")) == "already_shipped"


def test_an_already_cancelled_order_is_not_cancelled_again():
    assert orders_service.cancellable(
        _order(cancelled_at="2026-08-18T00:00:00Z")) == "already_cancelled"


@pytest.mark.parametrize("status", ["PAID", "PARTIALLY_PAID", "PARTIALLY_REFUNDED"])
def test_an_order_with_money_on_it_is_left_to_a_person(status):
    """The bot must not cancel its way into owing a refund."""
    assert orders_service.cancellable(_order(financial_status=status)) == "already_paid"


# --- doing it --------------------------------------------------------------


async def test_cancelling_asks_shopify_to_restock_and_not_refund(monkeypatch, shopify):
    _allow(monkeypatch, _order())

    await orders_service.cancel_order("#1008", contact="mona@example.com")

    assert len(shopify.calls) == 1
    call = shopify.calls[0]
    assert call["order_id"] == "gid://shopify/Order/555"
    # Nothing was paid on an unshipped COD order, so there is nothing to refund.
    assert call["refund"] is False
    # Without this the stock stays held for an order that no longer exists.
    assert call["restock"] is True
    assert call["reason"] == "CUSTOMER"


async def test_the_reason_reaches_the_staff_note(monkeypatch, shopify):
    _allow(monkeypatch, _order())

    await orders_service.cancel_order("#1008", contact="mona@example.com",
                                      reason_note="طلبت مقاس غلط")

    assert "طلبت مقاس غلط" in shopify.calls[0]["staff_note"]


async def test_the_customer_is_not_emailed_by_shopify(monkeypatch, shopify):
    """They are being told in the chat; a second message would be noise."""
    _allow(monkeypatch, _order())

    await orders_service.cancel_order("#1008", contact="mona@example.com")

    assert shopify.calls[0]["notify_customer"] is False


async def test_the_order_is_re_read_rather_than_assumed(monkeypatch, shopify):
    """Shopify cancels in a background job, so the result is read, not presumed."""
    _allow(monkeypatch, _order())

    order = await orders_service.cancel_order("#1008", contact="mona@example.com")

    assert order.is_cancelled is True


# --- what stops it ---------------------------------------------------------


async def test_a_shipped_order_is_refused_before_shopify_is_touched(monkeypatch, shopify):
    _allow(monkeypatch, _order(fulfillment_status="FULFILLED"))

    with pytest.raises(CancelRefused) as caught:
        await orders_service.cancel_order("#1008", contact="mona@example.com")

    assert caught.value.reason == "already_shipped"
    assert shopify.calls == []


async def test_an_unknown_order_or_wrong_contact_is_refused(monkeypatch, shopify):
    async def nothing(_number, _contact=None):
        return None

    monkeypatch.setattr(orders_service, "get_order_status", nothing)

    with pytest.raises(OrderRejected):
        await orders_service.cancel_order("#1008", contact="wrong@example.com")

    assert shopify.calls == []


async def test_the_contact_check_is_the_same_one_lookups_use(monkeypatch, shopify):
    """Cancelling must not be a way around the check that guards order lookups."""
    seen = {}

    async def status(number, contact=None):
        seen["number"], seen["contact"] = number, contact
        return None

    monkeypatch.setattr(orders_service, "get_order_status", status)

    with pytest.raises(OrderRejected):
        await orders_service.cancel_order("#1008", contact="mona@example.com")

    assert seen == {"number": "#1008", "contact": "mona@example.com"}


# --- the tool the model sees -----------------------------------------------


async def test_a_refusal_comes_back_as_a_bare_code(monkeypatch):
    """Tool results carry data, never instructions."""
    async def refuse(**_kwargs):
        raise CancelRefused("nope", "already_shipped")

    monkeypatch.setattr(orders_service, "cancel_order", refuse)

    result = await tools.dispatch("cancel_order",
                                  {"order_number": "#1008", "contact": "a@b.com"},
                                  context=tools.ToolContext(conversation_id="c1"))

    assert result == {"cancelled": False, "error": "already_shipped"}


async def test_not_yours_and_no_such_order_look_identical(monkeypatch):
    """Otherwise the tool becomes a way to discover which order numbers are real."""
    async def refuse(**_kwargs):
        raise OrderRejected("no such order")

    monkeypatch.setattr(orders_service, "cancel_order", refuse)

    result = await tools.dispatch("cancel_order",
                                  {"order_number": "#1008", "contact": "a@b.com"},
                                  context=tools.ToolContext(conversation_id="c1"))

    assert result == {"cancelled": False, "error": "not_found"}


async def test_a_successful_cancel_emails_the_owner(monkeypatch):
    """The owner's decision: the bot cancels, and the owner hears about it every time."""
    told = []

    async def cancelled(**_kwargs):
        return _order(cancelled_at="2026-08-19T12:00:00Z")

    async def notify(order, reason="", conversation_id=""):
        told.append((order.number, reason, conversation_id))
        return True

    monkeypatch.setattr(orders_service, "cancel_order", cancelled)
    monkeypatch.setattr(tools.notifications_service, "notify_order_cancelled", notify)

    result = await tools.dispatch(
        "cancel_order",
        {"order_number": "#1008", "contact": "a@b.com", "reason": "غيرت رأيي"},
        context=tools.ToolContext(conversation_id="c1"))

    assert result["cancelled"] is True and result["confirmed"] is True
    assert told == [("#1008", "غيرت رأيي", "c1")]


async def test_a_failed_email_does_not_unsay_the_cancellation(monkeypatch):
    """The order is already cancelled; claiming otherwise would be a lie."""
    async def cancelled(**_kwargs):
        return _order(cancelled_at="2026-08-19T12:00:00Z")

    async def explode(*_a, **_k):
        raise RuntimeError("mail server is gone")

    monkeypatch.setattr(orders_service, "cancel_order", cancelled)
    monkeypatch.setattr(tools.notifications_service, "notify_order_cancelled", explode)

    result = await tools.dispatch("cancel_order",
                                  {"order_number": "#1008", "contact": "a@b.com"},
                                  context=tools.ToolContext(conversation_id="c1"))

    assert result["cancelled"] is True


async def test_a_job_that_has_not_landed_is_reported_honestly(monkeypatch):
    """Shopify cancels asynchronously - "being cancelled" is not "cancelled"."""
    async def accepted(**_kwargs):
        return _order()  # re-read still shows it uncancelled

    async def notify(*_a, **_k):
        return True

    monkeypatch.setattr(orders_service, "cancel_order", accepted)
    monkeypatch.setattr(tools.notifications_service, "notify_order_cancelled", notify)

    result = await tools.dispatch("cancel_order",
                                  {"order_number": "#1008", "contact": "a@b.com"},
                                  context=tools.ToolContext(conversation_id="c1"))

    assert result["cancelled"] is True
    assert result["confirmed"] is False


# --- the policy the bot must state -----------------------------------------


def test_the_prompt_carries_the_exchange_terms():
    """A model that half-remembers a policy invents the other half."""
    from app.modules.chat import agent

    prompt = agent.build_system_prompt()

    assert "24 hours" in prompt
    assert "20 EGP" in prompt
    assert "original packaging" in prompt
    # Who pays, both ways.
    assert "the store covers it" in prompt
    assert "never waive a fee" in prompt


def test_the_prompt_no_longer_claims_it_cannot_cancel():
    from app.modules.chat import agent

    prompt = agent.build_system_prompt()

    assert "cannot change or cancel an order" not in prompt
    assert "CANCELLING AN ORDER" in prompt


def test_the_prompt_covers_a_refusal_it_does_not_have_a_name_for():
    """A live double-cancel returns shopify_refused - the model must not invent a reply."""
    from app.modules.chat import agent

    prompt = agent.build_system_prompt()

    assert "Any other error means it did not happen" in prompt
    assert "Never say an order was cancelled unless the result says it was" in prompt


def test_the_customer_is_not_told_where_the_stock_went():
    """It once said "القطعة رجعت تانية للمخزن" - true, internal, and not what was asked."""
    from app.modules.chat import agent

    prompt = agent.build_system_prompt()

    assert "Never mention stock, the warehouse, or the pieces going back on sale" in prompt
