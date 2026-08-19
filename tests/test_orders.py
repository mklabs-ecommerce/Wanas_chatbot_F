"""Orders module: payload conversion, and who is allowed to see an order.

The authorisation tests carry the weight here. Order numbers are sequential, so if the
contact check is ever weakened these are the tests that should fail.

Payloads are shaped like the real store's GraphQL response, so the suite never calls
Shopify.
"""

import pytest

from app.integrations.shopify.client import ShopifyUnavailable
from app.modules.orders import service
from app.modules.orders.schemas import Order

# The real store's #1003, with the contact details it actually carries: no email and no
# customer record, only a phone. An email-only check would have failed against it.
ORDER_1003 = {
    "id": "gid://shopify/Order/7472693346460",
    "name": "#1003",
    "createdAt": "2026-08-16T06:16:51Z",
    "cancelledAt": None,
    "cancelReason": None,
    "displayFinancialStatus": "PENDING",
    "displayFulfillmentStatus": "UNFULFILLED",
    "email": None,
    "phone": "+201067177129",
    "tags": ["cash-on-delivery", "chatbot", "whatsapp"],
    "totalPriceSet": {"shopMoney": {"amount": "650.0", "currencyCode": "EGP"}},
    "currentTotalPriceSet": {"shopMoney": {"amount": "650.0", "currencyCode": "EGP"}},
    "customer": None,
    "shippingAddress": {
        "name": "محمد فتحي",
        "phone": "+201067177129",
        "address1": "14 شارع النيل",
        "city": "Cairo",
        "province": "Cairo",
        "country": "Egypt",
    },
    "lineItems": {"nodes": [
        {"title": "BOXY WNS TEE", "quantity": 1, "variantTitle": "M / Black",
         "sku": "boxy-wns-tee-m-black"},
    ]},
    "fulfillments": [],
}

# A different customer's order, on the phone number Shopify wrongly returns when asked
# for #1003's number.
ORDER_1002 = {
    **ORDER_1003,
    "id": "gid://shopify/Order/7472658219164",
    "name": "#1002",
    "phone": "+201000000000",
    "cancelledAt": "2026-08-16T06:01:41Z",
    "cancelReason": "OTHER",
    "shippingAddress": {**ORDER_1003["shippingAddress"], "phone": "+201000000000"},
    "fulfillments": [],
}

SHIPPED = {
    **ORDER_1003,
    "name": "#1010",
    "email": "Customer@Example.COM",
    "phone": None,
    "displayFinancialStatus": "PAID",
    "displayFulfillmentStatus": "FULFILLED",
    "tags": [],
    "customer": {"email": "Customer@Example.COM", "phone": None},
    "fulfillments": [{
        "status": "SUCCESS",
        "createdAt": "2026-08-17T09:00:00Z",
        "estimatedDeliveryAt": "2026-08-20T09:00:00Z",
        "trackingInfo": [{"number": "EG123456789", "url": "https://track.example/EG123456789",
                          "company": "Bosta"}],
    }],
}


class FakeShopify:
    """Stands in for ShopifyClient; records queries and can be told to fail."""

    def __init__(self, nodes=None, error=None):
        self.nodes = nodes if nodes is not None else [ORDER_1003]
        self.error = error
        self.queries = []

    async def fetch_orders(self, query=None, first=10):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.nodes


@pytest.fixture(autouse=True)
def shopify(monkeypatch):
    fake = FakeShopify()
    monkeypatch.setattr(service, "_shopify", lambda: fake)
    return fake


# --- authorisation -------------------------------------------------------


async def test_the_right_phone_opens_the_order():
    order = await service.get_order_status("#1003", contact="+201067177129")
    assert order is not None
    assert order.number == "#1003"


async def test_the_wrong_phone_does_not(shopify):
    assert await service.get_order_status("#1003", contact="+201099999999") is None


async def test_an_order_number_alone_is_never_enough():
    """Order numbers are sequential; guessing one must not expose a customer's details."""
    assert await service.get_order_status("#1003") is None
    assert await service.get_order_status("#1003", contact="") is None


async def test_a_missing_order_and_a_wrong_contact_are_indistinguishable(shopify):
    """Otherwise the tool becomes a way to discover which order numbers are real."""
    shopify.nodes = []
    missing = await service.get_order_status("#9999", contact="+201067177129")

    shopify.nodes = [ORDER_1003]
    refused = await service.get_order_status("#1003", contact="+201099999999")

    assert missing is None and refused is None


async def test_verification_can_be_turned_off_for_a_closed_test_store(monkeypatch):
    monkeypatch.setattr(service.settings, "orders_require_contact_verification", False,
                        raising=False)
    order = await service.get_order_status("#1003")
    assert order is not None


# --- phone and email normalisation ---------------------------------------


@pytest.mark.parametrize("given", [
    "+201067177129",
    "00201067177129",
    "01067177129",
    "1067177129",
    "0106 717 7129",
    "+20 106-717-7129",
    "٠١٠٦٧١٧٧١٢٩",          # Arabic-Indic digits, as an Arabic keyboard produces them
])
async def test_every_spelling_of_the_same_phone_number_works(given):
    """Shopify stores +20...; customers type any of these."""
    assert await service.get_order_status("#1003", contact=given) is not None


@pytest.mark.parametrize("given", ["7129", "177129", "", "not a phone"])
async def test_too_few_digits_never_matches(given):
    """A short string must not act as a wildcard against the tail of a real number."""
    assert await service.get_order_status("#1003", contact=given) is None


async def test_email_matching_ignores_case(shopify):
    shopify.nodes = [SHIPPED]
    assert await service.get_order_status("#1010", contact="customer@example.com") is not None
    assert await service.get_order_status("#1010", contact="someone@example.com") is None


# --- Shopify's loose matching --------------------------------------------


async def test_an_order_shopify_returned_loosely_is_rejected(shopify):
    """Live finding: `phone:01067177129` also returns orders on +201000000000.

    Shopify's search is a relevance filter, not an equality check, so it can never be
    trusted to decide who owns an order.
    """
    shopify.nodes = [ORDER_1002, ORDER_1003]
    matched = await service.get_orders_by_customer("01067177129")

    assert [order.number for order in matched] == ["#1003"]


async def test_a_partially_matching_order_number_is_rejected(shopify):
    """`name:#100` also matches #1003; only the exact number may be returned."""
    shopify.nodes = [ORDER_1003]
    assert await service.get_order_status("#100", contact="+201067177129") is None


@pytest.mark.parametrize("given", ["#1003", "1003", "رقم 1003", " #1003 "])
async def test_order_numbers_are_accepted_however_the_customer_writes_them(given):
    assert await service.get_order_status(given, contact="+201067177129") is not None


async def test_customer_input_cannot_alter_the_shopify_query(shopify):
    await service.get_orders_by_customer('a@b.com" OR name:*')
    # Quotes, colons, spaces and wildcards are all stripped, so the injected clause
    # collapses into one harmless literal term.
    assert shopify.queries == ["email:a@b.comORname"]


# --- listing a customer's orders -----------------------------------------


async def test_orders_by_customer_returns_matches_newest_first(shopify):
    shopify.nodes = [ORDER_1003, {**ORDER_1003, "name": "#1000"}]
    assert [o.number for o in await service.get_orders_by_customer("01067177129")] == \
        ["#1003", "#1000"]


async def test_orders_by_customer_needs_a_contact(shopify):
    assert await service.get_orders_by_customer("") == []
    assert shopify.queries == []


async def test_an_email_contact_searches_the_email_field(shopify):
    shopify.nodes = [SHIPPED]
    await service.get_orders_by_customer("customer@example.com")
    assert shopify.queries == ["email:customer@example.com"]


# --- payload conversion ---------------------------------------------------


async def test_status_reaches_the_model_in_plain_words():
    order = await service.get_order_status("#1003", contact="+201067177129")
    payload = order.to_tool_dict()

    assert payload["status"] == "not shipped yet"
    assert payload["payment_status"] == "pending"
    assert payload["total"] == "650 EGP"
    assert payload["items"] == [{"title": "BOXY WNS TEE", "quantity": 1,
                                 "variant": "M / Black"}]


async def test_a_cancelled_order_says_so_rather_than_reporting_its_fulfillment(shopify):
    """#1002 is CANCELLED but still UNFULFILLED; "not shipped yet" would mislead."""
    shopify.nodes = [ORDER_1002]
    order = await service.get_order_status("#1002", contact="+201000000000")
    payload = order.to_tool_dict()

    assert payload["status"] == "cancelled"
    assert payload["cancelled_on"] == "2026-08-16"


async def test_tracking_and_delivery_estimate_are_passed_through(shopify):
    shopify.nodes = [SHIPPED]
    order = await service.get_order_status("#1010", contact="customer@example.com")
    payload = order.to_tool_dict()

    assert payload["status"] == "shipped"
    assert payload["tracking"] == {"carrier": "Bosta", "number": "EG123456789",
                                   "url": "https://track.example/EG123456789"}
    assert payload["estimated_delivery"] == "2026-08-20"


async def test_an_unshipped_order_offers_no_tracking_and_no_estimate():
    order = await service.get_order_status("#1003", contact="+201067177129")
    payload = order.to_tool_dict()

    assert "tracking" not in payload
    assert "estimated_delivery" not in payload


async def test_cash_on_delivery_is_derived_from_the_tag():
    order = await service.get_order_status("#1003", contact="+201067177129")
    assert order.to_tool_dict()["payment_method"] == "cash on delivery"


async def test_the_model_never_sees_contact_details_or_internal_fields():
    """The record holds a street address, staff tags and an internal note; none of it
    belongs in a chat window, and echoing a phone number back leaks it to whoever else
    is holding the conversation."""
    order = await service.get_order_status("#1003", contact="+201067177129")
    payload = order.to_tool_dict()

    assert "+201067177129" not in str(payload)
    assert "شارع النيل" not in str(payload)
    for field in ("phone", "email", "tags", "id", "note", "address", "customer"):
        assert field not in payload
    # City and country are kept: enough to confirm the delivery, without the street.
    assert payload["ships_to"] == "Cairo, Egypt"


async def test_line_items_are_dropped_from_a_list_of_orders():
    order = await service.get_order_status("#1003", contact="+201067177129")
    assert "items" not in order.to_tool_dict(include_items=False)


# --- failure --------------------------------------------------------------


async def test_a_shopify_failure_is_raised_not_answered_with_no_orders(shopify):
    """"You have no orders" would be a lie when Shopify is simply unreachable."""
    shopify.error = ShopifyUnavailable("503")

    with pytest.raises(service.OrdersUnavailable):
        await service.get_order_status("#1003", contact="+201067177129")
    with pytest.raises(service.OrdersUnavailable):
        await service.get_orders_by_customer("01067177129")


async def test_an_empty_order_number_never_reaches_shopify(shopify):
    assert await service.get_order_status("", contact="+201067177129") is None
    assert shopify.queries == []
