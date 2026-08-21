"""Paying online, through a Shopify checkout link.

The one thing that makes this safe is that the bot never touches a payment: it hands
over a link and Shopify does the rest. So most of what is asserted here is about the
gap between handing out a link and money actually arriving - nothing is ordered,
reserved or charged until the customer pays, and the bot must not claim otherwise.

The other half is what happens once they do pay: the draft becomes a real order of this
conversation, and everything that already follows an order picks it up.
"""

import pytest

from app.core.config import settings
from app.modules.catalog.schemas import Product, Variant
from app.modules.chat import agent, tools
from app.modules.orders import repository as orders_repository
from app.modules.orders import service as orders_service
from app.modules.orders.schemas import Draft, LineItem, Order
from app.modules.orders.service import OrderRejected, OrdersUnavailable, RequestedItem

CONVERSATION = "conv-online-1"


def _variant(**overrides) -> Variant:
    fields = dict(id="gid://shopify/ProductVariant/11", title="M / Brown", price="580",
                  currency="EGP", available=True,
                  options={"Size": "M", "Color": "Brown"})
    fields.update(overrides)
    return Variant(**fields)


def _product() -> Product:
    return Product(id="gid://shopify/Product/1", title="RINGER BOXY FIT TSHIRT",
                   handle="ringer", variants=[_variant()])


def _draft_node(**overrides) -> dict:
    node = {
        "id": "gid://shopify/DraftOrder/900",
        "name": "#D3",
        "invoiceUrl": "https://shop.myshopify.com/1234/invoices/abcdef",
        "status": "OPEN",
        "createdAt": "2026-08-19T10:00:00Z",
        "tags": ["online-payment", "chatbot", "web"],
        "currencyCode": "EGP",
        "totalPriceSet": {"shopMoney": {"amount": "580.00", "currencyCode": "EGP"}},
        "subtotalPriceSet": {"shopMoney": {"amount": "580.00", "currencyCode": "EGP"}},
        "totalShippingPriceSet": {"shopMoney": {"amount": "0.00", "currencyCode": "EGP"}},
        "order": None,
        "lineItems": {"nodes": [{"title": "RINGER BOXY FIT TSHIRT", "quantity": 1,
                                 "variantTitle": "M / Brown", "sku": None,
                                 "variant": {"id": _variant().id}}]},
    }
    node.update(overrides)
    return node


class _FakeClient:
    """Stands in for Shopify, and remembers exactly what it was sent."""

    def __init__(self, node=None, raises=None):
        self.created = []
        self.node = node or _draft_node()
        self.raises = raises
        self.fetched = []

    async def create_draft_order(self, draft):
        self.created.append(draft)
        if self.raises:
            raise self.raises
        return dict(self.node)

    async def fetch_draft_order(self, draft_id):
        self.fetched.append(draft_id)
        if self.node is None:
            return None
        return dict(self.node)


@pytest.fixture
def shopify(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(orders_service, "_shopify", lambda: client)
    return client


@pytest.fixture
def catalog(monkeypatch):
    async def resolve(_ref, size=None, color=None):
        return _product(), _variant()

    monkeypatch.setattr(orders_service.catalog_service, "resolve_variant", resolve)


def _items():
    return [RequestedItem(product="RINGER BOXY FIT TSHIRT", size="M", color="Brown")]


# --- a link is not an order ------------------------------------------------


async def test_a_new_link_is_not_paid(shopify, catalog):
    draft = await orders_service.create_draft_order(_items(),
                                                    conversation_id=CONVERSATION)
    assert draft.paid is False
    assert draft.order_number is None
    assert draft.checkout_url.startswith("https://")


async def test_the_tool_result_says_it_is_not_paid_and_carries_the_link(shopify, catalog):
    result = await tools.dispatch("create_payment_link",
                                  {"items": [{"product": "RINGER", "size": "M",
                                              "color": "Brown"}],
                                   "customer_confirmed": True},
                                  tools.ToolContext(conversation_id=CONVERSATION))
    assert result["created"] is True
    link = result["payment_link"]
    assert link["paid"] is False
    assert link["checkout_url"].startswith("https://")


async def test_the_total_is_named_as_the_goods_only(shopify, catalog):
    """A draft is priced before delivery is chosen; calling it "total" invites a lie."""
    draft = await orders_service.create_draft_order(_items(),
                                                    conversation_id=CONVERSATION)
    payload = draft.to_tool_dict()

    assert "total" not in payload
    assert payload["items_total"] == "580.00"
    assert payload["delivery_charged_at_checkout"] is True


def test_the_shopify_id_never_reaches_the_model():
    draft = Draft(id="gid://shopify/DraftOrder/900", name="#D3",
                  checkout_url="https://example.test/pay")
    payload = draft.to_tool_dict()

    assert "id" not in payload
    assert "name" not in payload
    assert "gid://" not in str(payload)


def test_the_draft_name_is_not_offered_as_an_order_number():
    """#D3 is not an order number, and staff cannot look one up by it."""
    draft = Draft(id="x", name="#D3", checkout_url="https://example.test/pay")
    assert "#D3" not in str(draft.to_tool_dict())


# --- what is sent to Shopify -----------------------------------------------


async def test_only_the_variant_and_quantity_are_sent_never_a_price(shopify, catalog):
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)
    line = shopify.created[0]["lineItems"][0]

    assert set(line) == {"variantId", "quantity", "requiresShipping"}
    assert line["requiresShipping"] is True


async def test_it_is_not_tagged_as_cash_on_delivery(shopify, catalog):
    """That tag means cash to collect. These are paid before the parcel moves."""
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION,
                                            channel="web")
    tags = shopify.created[0]["tags"]

    assert "cash-on-delivery" not in tags
    assert "chatbot" in tags and "online-payment" in tags


async def test_the_channel_is_tagged_from_the_request(shopify, catalog):
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION,
                                            channel="whatsapp")
    assert "whatsapp" in shopify.created[0]["tags"]


async def test_no_address_is_needed_because_the_checkout_collects_it(shopify, catalog):
    draft = await orders_service.create_draft_order(_items(),
                                                    conversation_id=CONVERSATION)
    assert draft.checkout_url
    assert "shippingAddress" not in shopify.created[0]


async def test_an_address_already_given_is_passed_through(shopify, catalog):
    await orders_service.create_draft_order(
        _items(), customer_name="Mona Adel", phone="01021255687",
        address1="12 Talaat Harb", city="Shibin El Kom", governorate="Monufia",
        conversation_id=CONVERSATION)
    address = shopify.created[0]["shippingAddress"]

    assert address["address1"] == "12 Talaat Harb"
    assert address["provinceCode"] == "MNF"
    # Shopify refuses a local spelling outright.
    assert address["phone"] == "+201021255687"


async def test_half_an_address_is_sent_as_none(shopify, catalog):
    """A partly filled checkout reads as complete and gets clicked past."""
    await orders_service.create_draft_order(_items(), address1="12 Talaat Harb",
                                            conversation_id=CONVERSATION)
    assert "shippingAddress" not in shopify.created[0]


# --- what is refused -------------------------------------------------------


async def test_a_sold_out_size_cannot_be_linked(monkeypatch, shopify):
    async def resolve(_ref, size=None, color=None):
        raise orders_service.catalog_service.VariantNotFound("Size XL is sold out",
                                                             available=[])

    monkeypatch.setattr(orders_service.catalog_service, "resolve_variant", resolve)
    with pytest.raises(OrderRejected):
        await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)
    assert shopify.created == []


async def test_an_empty_basket_is_refused(shopify, catalog):
    with pytest.raises(OrderRejected):
        await orders_service.create_draft_order([], conversation_id=CONVERSATION)


async def test_an_incomplete_phone_is_refused(shopify, catalog):
    with pytest.raises(OrderRejected):
        await orders_service.create_draft_order(_items(), phone="0102",
                                                conversation_id=CONVERSATION)


async def test_a_store_with_online_payment_off_refuses(monkeypatch, shopify, catalog):
    monkeypatch.setattr(settings, "online_payment_enabled", False, raising=False)
    with pytest.raises(OrdersUnavailable):
        await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)


def test_the_tool_is_not_even_offered_when_online_payment_is_off(monkeypatch):
    """The model cannot offer a way to pay that the store does not have."""
    monkeypatch.setattr(settings, "online_payment_enabled", False, raising=False)
    assert "create_payment_link" not in tools.names()
    assert "create_payment_link" not in [d["name"] for d in tools.declarations()]


async def test_calling_a_switched_off_tool_is_refused_rather_than_run(monkeypatch):
    monkeypatch.setattr(settings, "online_payment_enabled", False, raising=False)
    result = await tools.dispatch("create_payment_link",
                                  {"items": [{"product": "x", "size": "M",
                                              "color": "Brown"}],
                                   "customer_confirmed": True},
                                  tools.ToolContext(conversation_id=CONVERSATION))
    assert "error" in result


async def test_an_unconfirmed_basket_is_not_linked(shopify, catalog):
    result = await tools.dispatch("create_payment_link",
                                  {"items": [{"product": "x", "size": "M",
                                              "color": "Brown"}]},
                                  tools.ToolContext(conversation_id=CONVERSATION))
    assert result == {"error": "not_confirmed"}
    assert shopify.created == []


async def test_a_draft_with_no_link_is_a_failure_not_a_success(monkeypatch, catalog):
    """A link the customer cannot open is worse than an honest failure."""
    client = _FakeClient(node=_draft_node(invoiceUrl=""))
    monkeypatch.setattr(orders_service, "_shopify", lambda: client)

    with pytest.raises(OrdersUnavailable):
        await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)


# --- one basket, one link --------------------------------------------------


async def test_the_same_basket_reuses_the_link_it_already_gave(shopify, catalog):
    """Two links for one basket is an invitation to pay twice."""
    first = await orders_service.create_draft_order(_items(),
                                                    conversation_id=CONVERSATION)
    second = await orders_service.create_draft_order(_items(),
                                                     conversation_id=CONVERSATION)

    assert second.checkout_url == first.checkout_url
    assert len(shopify.created) == 1


async def test_a_different_basket_gets_its_own_link(shopify, catalog, monkeypatch):
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)

    async def resolve(_ref, size=None, color=None):
        return _product(), _variant(id="gid://shopify/ProductVariant/22", title="L / Navy")

    monkeypatch.setattr(orders_service.catalog_service, "resolve_variant", resolve)
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)
    assert len(shopify.created) == 2


async def test_a_link_that_was_already_paid_is_not_reused(shopify, catalog):
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)
    shopify.node = _draft_node(status="COMPLETED", order={"id": "gid://shopify/Order/7",
                                                          "name": "#1011"})

    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)
    assert len(shopify.created) == 2


# --- finding out that they paid --------------------------------------------


def _paid_order(**overrides) -> Order:
    fields = dict(id="gid://shopify/Order/7", number="#1011", financial_status="PAID",
                  fulfillment_status="UNFULFILLED", cash_on_delivery=False,
                  total="650", currency="EGP",
                  items=[LineItem(title="RINGER BOXY FIT TSHIRT", quantity=1,
                                  variant_title="M / Brown")])
    fields.update(overrides)
    return Order(**fields)


def _paid(monkeypatch, shopify, order=None, readable=True):
    """Mark the draft paid. ``readable=False`` is the order search lagging behind."""
    shopify.node = _draft_node(status="COMPLETED",
                               order={"id": "gid://shopify/Order/7", "name": "#1011"})

    async def by_id(_order_id):
        return (order or _paid_order()) if readable else None

    async def staff(_number):
        return (order or _paid_order()) if readable else None

    monkeypatch.setattr(orders_service, "lookup_by_id", by_id)
    monkeypatch.setattr(orders_service, "lookup_for_staff", staff)


async def test_an_unpaid_link_produces_no_news(shopify, catalog):
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)
    assert await orders_service.payment_news(CONVERSATION) == []


async def test_a_paid_link_is_reported_once(monkeypatch, shopify, catalog):
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)
    _paid(monkeypatch, shopify)

    news = await orders_service.payment_news(CONVERSATION)
    assert [order.number for order in news] == ["#1011"]

    orders_service.mark_payment_announced(CONVERSATION, "#1011")
    assert await orders_service.payment_news(CONVERSATION) == []


async def test_paying_makes_it_an_order_of_this_conversation(monkeypatch, shopify, catalog):
    """So the shipping notice, the feedback ask and the dashboard all pick it up."""
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)
    _paid(monkeypatch, shopify)
    await orders_service.payment_news(CONVERSATION)

    assert orders_service.order_numbers_for_conversation(CONVERSATION) == ["#1011"]
    assert orders_service.pieces_ordered_in_conversation(CONVERSATION) == 1


async def test_a_paid_link_stops_being_re_read_from_shopify(monkeypatch, shopify, catalog):
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)
    _paid(monkeypatch, shopify)
    await orders_service.payment_news(CONVERSATION)
    orders_service.mark_payment_announced(CONVERSATION, "#1011")

    shopify.fetched.clear()
    await orders_service.payment_news(CONVERSATION)
    assert shopify.fetched == []


async def test_a_link_deleted_in_the_admin_is_given_up_on(shopify, catalog):
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)
    shopify.node = None

    assert await orders_service.payment_news(CONVERSATION) == []
    shopify.fetched.clear()
    await orders_service.payment_news(CONVERSATION)
    assert shopify.fetched == []


async def test_an_order_cancelled_before_they_were_told_is_not_announced(
        monkeypatch, shopify, catalog):
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)
    _paid(monkeypatch, shopify, order=_paid_order(cancelled_at="2026-08-19T12:00:00Z"))

    assert await orders_service.payment_news(CONVERSATION) == []


async def test_shopify_being_down_costs_the_news_not_the_conversation(monkeypatch, shopify,
                                                                     catalog):
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)

    async def explode(_draft_id):
        raise RuntimeError("shopify is unreachable")

    monkeypatch.setattr(shopify, "fetch_draft_order", explode)
    assert await orders_service.payment_news(CONVERSATION) == []


async def test_a_conversation_that_never_paid_online_costs_nothing():
    assert await orders_service.payment_news("some-other-conversation") == []


# --- what the customer is told ---------------------------------------------


def test_the_paid_note_carries_the_order_number_and_says_it_is_placed():
    prompt = agent.build_system_prompt(paid_orders=[_paid_order()])

    assert "#1011" in prompt
    assert "payment was received" in prompt
    assert "Do not repeat this in later messages" in prompt


def test_there_is_no_paid_note_when_nothing_was_paid():
    assert "ONLINE PAYMENT HAS GONE THROUGH" not in agent.build_system_prompt()


def test_the_prompt_forbids_asking_for_card_details():
    prompt = agent.build_system_prompt()

    assert "Never ask for a card number" in prompt
    assert "A payment link is not an order" in prompt


def test_the_prompt_says_a_closed_storefront_does_not_close_the_checkout(monkeypatch):
    """The link is on the shop's domain but is not the shop, and it does open."""
    from app.modules.catalog import service as catalog_service

    monkeypatch.setattr(catalog_service, "storefront_is_open", lambda: False)
    prompt = agent.build_system_prompt()

    assert "never send them there to order" in prompt
    assert "That is a payment page, not the shop" in prompt


# --- the local record ------------------------------------------------------


def test_a_link_with_no_conversation_is_not_recorded():
    orders_repository.link_draft("", "gid://shopify/DraftOrder/1")
    orders_repository.link_draft("c1", "")
    assert orders_repository.draft_count() == 0


def test_recording_the_same_link_twice_keeps_one_row():
    orders_repository.link_draft("c1", "gid://shopify/DraftOrder/1", "#D1")
    orders_repository.link_draft("c1", "gid://shopify/DraftOrder/1", "#D1")
    assert orders_repository.draft_count() == 1


# --- what the courier is told ----------------------------------------------


async def test_the_staff_note_does_not_say_cash_on_delivery(shopify, catalog):
    """It is already paid. A note saying otherwise gets money asked for at the door."""
    await orders_service.create_draft_order(_items(), phone="01021255687",
                                            conversation_id=CONVERSATION)
    note = shopify.created[0]["note"]

    assert "Cash on delivery" not in note
    assert "Paid online" in note


async def test_a_cod_order_still_says_cash_on_delivery():
    note = orders_service._staff_note("web", "01021255687", None)
    assert "Cash on delivery" in note


async def test_a_link_is_not_reused_once_the_price_has_changed(shopify, catalog,
                                                               monkeypatch):
    """A draft holds the price it was made at; reusing a stale one undersells."""
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)

    async def dearer(_ref, size=None, color=None):
        return _product(), _variant(price="640")

    monkeypatch.setattr(orders_service.catalog_service, "resolve_variant", dearer)
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)
    assert len(shopify.created) == 2


async def test_an_old_link_at_the_same_price_is_still_reused(shopify, catalog):
    """No time limit on purpose: an old link still takes money, so two is one too many."""
    shopify.node = _draft_node(createdAt="2026-01-01T09:00:00Z")
    first = await orders_service.create_draft_order(_items(),
                                                    conversation_id=CONVERSATION)
    second = await orders_service.create_draft_order(_items(),
                                                     conversation_id=CONVERSATION)

    assert second.checkout_url == first.checkout_url
    assert len(shopify.created) == 1


async def test_the_owner_can_see_a_link_that_was_never_paid(shopify, catalog):
    """Otherwise a customer who reached a checkout and stopped is invisible."""
    from app.modules.admin.conversations import service as admin_conversations_service
    from app.modules.chat import repository as chat_repository

    cid = chat_repository.ensure_conversation(None, channel="web")
    chat_repository.add_message(cid, chat_repository.ROLE_USER, "عايز أدفع أونلاين")
    await orders_service.create_draft_order(_items(), conversation_id=cid)

    row = admin_conversations_service.list_conversations(None)["conversations"][0]
    assert row["unpaid_link_count"] == 1
    assert row["order_count"] == 0


async def test_a_paid_link_stops_counting_as_unpaid(monkeypatch, shopify, catalog):
    from app.modules.admin.conversations import service as admin_conversations_service
    from app.modules.chat import repository as chat_repository

    cid = chat_repository.ensure_conversation(None, channel="web")
    chat_repository.add_message(cid, chat_repository.ROLE_USER, "hi")
    await orders_service.create_draft_order(_items(), conversation_id=cid)
    _paid(monkeypatch, shopify)
    await orders_service.payment_news(cid)

    row = admin_conversations_service.list_conversations(None)["conversations"][0]
    assert row["unpaid_link_count"] == 0
    assert row["order_count"] == 1


# --- when an online order counts as arrived --------------------------------


def test_a_prepaid_order_is_not_arrived_just_because_it_is_paid():
    """Online payment happens at checkout, days before the parcel moves."""
    assert _paid_order().reached_the_customer is False


def test_a_prepaid_order_is_not_arrived_just_because_it_shipped():
    order = _paid_order(fulfillment_status="FULFILLED")
    assert order.reached_the_customer is False


def test_a_prepaid_order_is_arrived_when_the_carrier_says_so():
    order = _paid_order(fulfillment_status="FULFILLED", carrier_delivered=True)
    assert order.reached_the_customer is True


def test_a_cancelled_order_is_never_arrived():
    order = _paid_order(carrier_delivered=True, cancelled_at="2026-08-19T12:00:00Z")
    assert order.reached_the_customer is False


def test_the_carrier_report_is_read_from_shopifys_own_fields():
    assert orders_service._carrier_delivered([{"deliveredAt": "2026-08-19T12:00:00Z"}])
    assert orders_service._carrier_delivered([{"displayStatus": "DELIVERED"}])
    assert not orders_service._carrier_delivered([{"displayStatus": "FULFILLED"}])
    assert not orders_service._carrier_delivered([])


# --- the order search lags behind reality ----------------------------------


async def test_a_paid_order_is_read_by_id_not_searched_for(monkeypatch, shopify, catalog):
    """Shopify's order search cannot see an order this new - measured, not assumed."""
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)
    _paid(monkeypatch, shopify)

    searched = []

    async def staff(number):
        searched.append(number)
        return None

    monkeypatch.setattr(orders_service, "lookup_for_staff", staff)
    news = await orders_service.payment_news(CONVERSATION)

    assert [order.number for order in news] == ["#1011"]
    assert searched == []


async def test_an_order_that_cannot_be_read_yet_is_tried_again(monkeypatch, shopify,
                                                               catalog):
    """Marking it told here is how the news was lost once - it must not happen."""
    await orders_service.create_draft_order(_items(), conversation_id=CONVERSATION)
    _paid(monkeypatch, shopify, readable=False)

    assert await orders_service.payment_news(CONVERSATION) == []

    _paid(monkeypatch, shopify, readable=True)
    news = await orders_service.payment_news(CONVERSATION)
    assert [order.number for order in news] == ["#1011"]
