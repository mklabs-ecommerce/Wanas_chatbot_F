"""Creating a cash-on-delivery order.

This is the first thing the bot does that is not reversible by reloading the page: it
creates a real order that staff will pack and a courier will deliver. So the tests here
are weighted towards everything that must *stop* an order - a missing address, a size
that just sold out, the same order arriving twice - rather than the happy path.
"""

import copy

import pytest

from app.integrations.shopify.client import ShopifyRejected, ShopifyUnavailable
from app.modules.catalog import service as catalog_service
from app.modules.chat import tools
from app.modules.orders import service, shipping
from app.modules.orders.service import OrderRejected, OrdersUnavailable, RequestedItem

# Shaped like the real store's General profile: one domestic zone covering the Egyptian
# governorates, one active flat rate.
DELIVERY_PROFILES = [{
    "name": "General profile", "default": True,
    "profileLocationGroups": [{"locationGroupZones": {"nodes": [{
        "zone": {"name": "Domestic", "countries": [{
            "code": {"countryCode": "EG", "restOfWorld": False},
            "provinces": [{"code": code} for code in ("C", "GZ", "MNF", "ALX")],
        }]},
        "methodDefinitions": {"nodes": [{
            "name": "Standard", "active": True,
            "rateProvider": {"__typename": "DeliveryRateDefinition",
                             "price": {"amount": "118.0", "currencyCode": "EGP"}},
            "methodConditions": [],
        }]},
    }]}}],
}]


def _variant(size, color, price="580.00", available=True):
    return {
        "id": "gid://shopify/ProductVariant/" + size + color,
        "title": size + " / " + color, "sku": (size + "-" + color).lower(),
        "price": price, "availableForSale": available, "inventoryQuantity": 4,
        "selectedOptions": [{"name": "Size", "value": size},
                            {"name": "Color", "value": color}],
    }


PRODUCT = {
    "id": "gid://shopify/Product/1", "title": "RINGER BOXY FIT TSHIRT",
    "handle": "ringer-boxy-fit-tshirt", "productType": "T-Shirts", "tags": [],
    "status": "ACTIVE", "onlineStoreUrl": None, "totalInventory": 8, "description": "",
    "featuredImage": None,
    "priceRangeV2": {"minVariantPrice": {"amount": "500.00", "currencyCode": "EGP"},
                     "maxVariantPrice": {"amount": "580.00", "currencyCode": "EGP"}},
    "variants": {"nodes": [
        _variant("M", "Brown"), _variant("L", "Brown"),
        _variant("M", "Burgundy", price="500.00", available=False),
        _variant("XL", "Burgundy", price="500.00"),
    ]},
}


def _created_order(name="#1004", line_items=None, tags=None, created_at="2026-08-18T10:00:00Z"):
    return {
        "id": "gid://shopify/Order/1", "name": name, "createdAt": created_at,
        "cancelledAt": None, "cancelReason": None,
        "displayFinancialStatus": "PENDING", "displayFulfillmentStatus": "UNFULFILLED",
        "email": None, "phone": "+201067177129",
        "tags": tags if tags is not None else ["cash-on-delivery", "chatbot", "web"],
        "totalPriceSet": {"shopMoney": {"amount": "698.0", "currencyCode": "EGP"}},
        "currentTotalPriceSet": {"shopMoney": {"amount": "698.0", "currencyCode": "EGP"}},
        "subtotalPriceSet": {"shopMoney": {"amount": "580.0", "currencyCode": "EGP"}},
        "totalShippingPriceSet": {"shopMoney": {"amount": "118.0", "currencyCode": "EGP"}},
        "shippingLine": {"title": "Standard"},
        "customer": None,
        "shippingAddress": {"name": "Mona Hassan", "phone": "+201067177129",
                            "city": "Cairo", "province": "Cairo", "country": "Egypt"},
        "lineItems": {"nodes": line_items if line_items is not None else [
            {"title": "RINGER BOXY FIT TSHIRT", "quantity": 1,
             "variantTitle": "L / Brown", "sku": "l-brown"}]},
        "fulfillments": [],
    }


class FakeShopify:
    """Records what would have been sent, and never talks to Shopify."""

    def __init__(self):
        self.created = []
        self.orders = []
        self.create_error = None
        self.fetch_error = None
        self.profiles = copy.deepcopy(DELIVERY_PROFILES)
        self.rates_error = None
        self.customers = []
        self.created_customers = []
        self.customer_error = None

    async def fetch_delivery_rates(self):
        if self.rates_error:
            raise self.rates_error
        return self.profiles

    async def find_customers(self, query):
        if self.customer_error:
            raise self.customer_error
        return list(self.customers)

    async def create_customer(self, customer):
        if self.customer_error:
            raise self.customer_error
        self.created_customers.append(customer)
        return {"id": "gid://shopify/Customer/999"}

    async def fetch_all_products(self, **_kwargs):
        return [PRODUCT]

    async def fetch_orders(self, query=None, first=10):
        if self.fetch_error:
            raise self.fetch_error
        return list(self.orders)

    async def create_order(self, order, options=None):
        if self.create_error:
            raise self.create_error
        self.created.append({"order": order, "options": options})
        return _created_order()


@pytest.fixture(autouse=True)
def shopify(monkeypatch):
    fake = FakeShopify()
    catalog_service.clear_cache()
    shipping.clear_cache()
    monkeypatch.setattr(service, "_shopify", lambda: fake)
    monkeypatch.setattr(catalog_service, "_shopify", lambda: fake)
    yield fake
    catalog_service.clear_cache()
    shipping.clear_cache()


GOOD = dict(
    customer_name="Mona Hassan",
    phone="01067177129",
    address1="14 Nile Street",
    city="Cairo",
)


def _items(size="L", color="Brown", quantity=1):
    return [RequestedItem(product="RINGER BOXY FIT TSHIRT", size=size, color=color,
                          quantity=quantity)]


# --- the store's convention ----------------------------------------------


async def test_the_order_is_pending_and_tagged_the_way_staff_filter(shopify):
    await service.create_cod_order(_items(), **GOOD)
    sent = shopify.created[0]["order"]

    assert sent["financialStatus"] == "PENDING"
    assert set(sent["tags"]) == {"cash-on-delivery", "chatbot", "web"}


async def test_the_channel_tag_follows_the_conversation_not_the_model(shopify):
    """The existing orders say "whatsapp" because that is where they came from.

    Repeating that on a web order would misfile it, and channel attribution is one of the
    things this build is meant to get right.
    """
    await service.create_cod_order(_items(), channel="whatsapp", **GOOD)
    assert "whatsapp" in shopify.created[0]["order"]["tags"]
    assert "web" not in shopify.created[0]["order"]["tags"]


async def test_stock_is_taken_without_ever_overselling(shopify):
    await service.create_cod_order(_items(), **GOOD)
    options = shopify.created[0]["options"]

    # BYPASS would let the shop sell what it does not have.
    assert options["inventoryBehaviour"] == "DECREMENT_OBEYING_POLICY"


async def test_the_receipt_setting_is_passed_to_shopify(shopify):
    """Whether customers get a confirmation email is the owner's call; they asked for it
    on (2026-08-18). Shopify only sends when an email address was actually given."""
    await service.create_cod_order(_items(), **GOOD)
    assert shopify.created[0]["options"]["sendReceipt"] is True


async def test_the_price_is_never_sent_only_the_variant(shopify):
    """Otherwise a bug or a prompt injection could set the price of a real sale."""
    await service.create_cod_order(_items(), **GOOD)
    lines = shopify.created[0]["order"]["lineItems"]

    assert lines == [{"variantId": "gid://shopify/ProductVariant/LBrown", "quantity": 1,
                      "requiresShipping": True}]
    assert "priceSet" not in str(lines)


async def test_the_delivery_address_is_carried_across(shopify):
    await service.create_cod_order(_items(), address2="Flat 3", **GOOD)
    address = shopify.created[0]["order"]["shippingAddress"]

    assert address["address1"] == "14 Nile Street"
    assert address["address2"] == "Flat 3"
    assert address["city"] == "Cairo"
    assert address["countryCode"] == "EG"
    # Converted on the way out; Shopify rejects the local spelling.
    assert address["phone"] == "+201067177129"


async def test_staff_get_a_note_saying_cash_is_owed(shopify):
    await service.create_cod_order(_items(), note="Call before arriving", **GOOD)
    note = shopify.created[0]["order"]["note"]

    assert "Cash on delivery" in note
    assert "Call before arriving" in note


# --- what must stop an order ---------------------------------------------


@pytest.mark.parametrize("missing", ["customer_name", "phone", "address1", "city"])
async def test_a_missing_delivery_detail_stops_the_order(shopify, missing):
    """No checkout page collected these, so nothing else will catch a gap."""
    details = dict(GOOD, **{missing: ""})
    with pytest.raises(OrderRejected):
        await service.create_cod_order(_items(), **details)
    assert shopify.created == []


@pytest.mark.parametrize("phone", ["12345", "not a phone", "0106"])
async def test_an_incomplete_phone_stops_the_order(shopify, phone):
    """A COD parcel with an unreachable number cannot be delivered or collected on."""
    with pytest.raises(OrderRejected):
        await service.create_cod_order(_items(), **dict(GOOD, phone=phone))
    assert shopify.created == []


async def test_a_size_that_just_sold_out_stops_the_order(shopify):
    """The catalog is cached for minutes; stock can go in that time."""
    with pytest.raises(OrderRejected) as raised:
        await service.create_cod_order(_items(size="M", color="Burgundy"), **GOOD)

    assert "sold out" in str(raised.value)
    # What is left is carried back, so the customer can be offered it.
    assert raised.value.detail["available"]
    assert shopify.created == []


async def test_a_product_that_does_not_exist_stops_the_order(shopify):
    with pytest.raises(OrderRejected):
        await service.create_cod_order(
            [RequestedItem(product="WANAS VARSITY JACKET", size="M", color="Black")],
            **GOOD)
    assert shopify.created == []


async def test_an_order_without_a_chosen_size_stops_rather_than_guessing(shopify):
    """Two variants match "brown"; picking one for the customer is not ours to do."""
    with pytest.raises(OrderRejected) as raised:
        await service.create_cod_order(_items(size=None), **GOOD)
    assert "size and colour are needed" in str(raised.value)


@pytest.mark.parametrize("quantity", [0, -1, 999, "many", None])
async def test_an_impossible_quantity_stops_the_order(shopify, quantity):
    with pytest.raises(OrderRejected):
        await service.create_cod_order(_items(quantity=quantity), **GOOD)
    assert shopify.created == []


async def test_an_empty_basket_stops_the_order(shopify):
    with pytest.raises(OrderRejected):
        await service.create_cod_order([], **GOOD)


async def test_too_many_different_items_stops_the_order(shopify):
    with pytest.raises(OrderRejected):
        await service.create_cod_order(_items() * 6, **GOOD)


async def test_shopify_refusing_is_reported_as_a_refusal_not_an_outage(shopify):
    shopify.create_error = ShopifyRejected("Shopify refused the order: not enough stock")
    with pytest.raises(OrderRejected):
        await service.create_cod_order(_items(), **GOOD)


async def test_shopify_being_down_is_reported_as_an_outage(shopify):
    shopify.create_error = ShopifyUnavailable("503")
    with pytest.raises(OrdersUnavailable):
        await service.create_cod_order(_items(), **GOOD)


# --- the same order arriving twice ---------------------------------------


async def test_an_identical_order_moments_old_is_reused_not_repeated(shopify, monkeypatch):
    """Usually the model calling the tool twice in one turn. A duplicate parcel is real
    money, so the first order is returned instead of a second being created."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    shopify.orders = [_created_order(created_at=now)]

    order = await service.create_cod_order(_items(), **GOOD)

    assert order.number == "#1004"
    assert shopify.created == []


async def test_a_different_order_from_the_same_customer_still_goes_through(shopify):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    shopify.orders = [_created_order(created_at=now, line_items=[
        {"title": "WANAS HOODIE", "quantity": 1, "variantTitle": "M / Olive", "sku": "x"}])]

    await service.create_cod_order(_items(), **GOOD)
    assert len(shopify.created) == 1


async def test_an_older_identical_order_does_not_block_a_new_one(shopify):
    """A customer genuinely reordering the same piece next week must not be refused."""
    shopify.orders = [_created_order(created_at="2026-08-01T10:00:00Z")]

    await service.create_cod_order(_items(), **GOOD)
    assert len(shopify.created) == 1


async def test_a_cancelled_order_is_not_treated_as_a_duplicate(shopify):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cancelled = _created_order(created_at=now)
    cancelled["cancelledAt"] = now
    shopify.orders = [cancelled]

    await service.create_cod_order(_items(), **GOOD)
    assert len(shopify.created) == 1


async def test_a_failed_duplicate_check_does_not_block_the_order(shopify):
    """The check is a safeguard, not a precondition."""
    shopify.fetch_error = ShopifyUnavailable("503")
    await service.create_cod_order(_items(), **GOOD)
    assert len(shopify.created) == 1


# --- through the tools layer ---------------------------------------------


def _arguments(**overrides):
    arguments = {
        "items": [{"product": "RINGER BOXY FIT TSHIRT", "size": "L", "color": "Brown",
                   "quantity": 1}],
        "customer_name": "Mona Hassan", "phone": "01067177129",
        "address1": "14 Nile Street", "city": "Cairo", "customer_confirmed": True,
    }
    arguments.update(overrides)
    return arguments


async def test_an_unconfirmed_order_never_reaches_shopify(shopify):
    result = await tools.dispatch("create_cod_order", _arguments(customer_confirmed=False))

    assert result == {"error": "not_confirmed"}
    assert shopify.created == []


async def test_the_tool_creates_the_order_and_reports_its_number(shopify):
    result = await tools.dispatch("create_cod_order", _arguments(),
                                  context=tools.ToolContext(channel="web"))

    assert result["created"] is True
    assert result["order"]["order_number"] == "#1004"
    assert result["order"]["payment_method"] == "cash on delivery"


async def test_the_model_cannot_choose_the_channel_tag(shopify):
    """It is a fact about the request, so it comes from the context, not the arguments."""
    await tools.dispatch("create_cod_order", _arguments(channel="whatsapp"),
                         context=tools.ToolContext(channel="web"))

    assert "web" in shopify.created[0]["order"]["tags"]
    assert "whatsapp" not in shopify.created[0]["order"]["tags"]


async def test_a_refusal_comes_back_as_data_the_bot_can_explain(shopify):
    result = await tools.dispatch("create_cod_order", _arguments(
        items=[{"product": "RINGER BOXY FIT TSHIRT", "size": "M", "color": "Burgundy"}]))

    assert result["created"] is False
    assert result["error"] == "rejected"
    assert "available" in result


async def test_order_results_carry_data_only_never_instructions(shopify):
    shopify.create_error = ShopifyUnavailable("503")
    results = [
        await tools.dispatch("create_cod_order", _arguments(customer_confirmed=False)),
        await tools.dispatch("create_cod_order", _arguments()),
    ]
    for result in results:
        flattened = str(result).lower()
        for directive in ("tell the customer", "you should", "ask the customer",
                          "instruction", "please confirm"):
            assert directive not in flattened, result


# --- the confirmation rule lives in the prompt ---------------------------


def test_the_prompt_requires_a_read_back_before_any_order_is_created():
    """The tool's customer_confirmed flag is filled in by the model, so it proves
    nothing on its own. This wording is the actual control."""
    from app.modules.chat import agent

    prompt = agent.build_system_prompt()
    assert "read the whole order back" in prompt
    assert "Never set it true in the same message where you asked" in prompt
    assert "must have \ncome from the customer in this conversation" in prompt or \
        "come from the customer in this conversation" in prompt


def test_the_prompt_forbids_inventing_delivery_details():
    from app.modules.chat import agent

    prompt = agent.build_system_prompt()
    assert 'Never invent, assume, autocomplete or "tidy up"' in prompt
    assert "Never create a second order for the same request" in prompt


# --- the phone number Shopify will accept --------------------------------


@pytest.mark.parametrize("given", [
    "01067177129",        # how an Egyptian customer writes it
    "+201067177129",      # already international
    "00201067177129",     # international with the 00 prefix
    "1067177129",         # bare, no leading zero
    "0106 717 7129",      # spaced
    "010-6717-7129",      # dashed
    "٠١٠٦٧١٧٧١٢٩",         # Arabic-Indic digits
])
def test_every_local_spelling_becomes_the_international_form(given):
    """Found on the very first real order: Shopify answers "Phone is invalid" for
    01000000000, which is exactly how customers write their own number."""
    assert service.to_e164(given) == "+201067177129"


def test_an_empty_phone_stays_empty():
    assert service.to_e164("") == ""


async def test_shopify_is_sent_the_international_number(shopify):
    await service.create_cod_order(_items(), **dict(GOOD, phone="01067177129"))
    sent = shopify.created[0]["order"]

    assert sent["phone"] == "+201067177129"
    assert sent["shippingAddress"]["phone"] == "+201067177129"


async def test_the_staff_note_keeps_what_the_customer_actually_typed(shopify):
    """Staff read this back to the customer, who will not recognise +20."""
    await service.create_cod_order(_items(), **dict(GOOD, phone="01067177129"))
    assert "Phone: 01067177129" in shopify.created[0]["order"]["note"]


# --- the fields that came back empty on a real order ---------------------
#
# Order #1005 reached the owner reading "Government is missing", "No customer" and
# "Shipping is not required". Each of those is one missing field in the payload.


async def test_the_governorate_reaches_shopify_as_a_province_code(shopify):
    """Free text is not enough - without the code the order says "Government is missing"."""
    await service.create_cod_order(_items(), governorate="Monufia", **GOOD)
    assert shopify.created[0]["order"]["shippingAddress"]["provinceCode"] == "MNF"


async def test_an_arabic_governorate_is_understood(shopify):
    """The order that exposed this was addressed to المنوفية."""
    await service.create_cod_order(_items(), governorate="المنوفية", **GOOD)
    assert shopify.created[0]["order"]["shippingAddress"]["provinceCode"] == "MNF"


async def test_the_city_is_used_when_no_governorate_was_given(shopify):
    """Better a province inferred from "Cairo" than an order staff cannot ship."""
    await service.create_cod_order(_items(), **GOOD)
    assert shopify.created[0]["order"]["shippingAddress"]["provinceCode"] == "C"


async def test_an_unrecognised_governorate_does_not_lose_the_order(shopify):
    """A missing field is an annoyance for staff; a refused order is a lost sale."""
    details = dict(GOOD, city="Nowhere")
    await service.create_cod_order(_items(), governorate="Atlantis", **details)

    address = shopify.created[0]["order"]["shippingAddress"]
    assert "provinceCode" not in address
    assert len(shopify.created) == 1


async def test_the_order_requires_shipping(shopify):
    """orderCreate defaults this to false, so a real order claimed it needed no
    delivery even though every line is a physical garment."""
    await service.create_cod_order(_items(), **GOOD)
    assert all(line["requiresShipping"] is True
               for line in shopify.created[0]["order"]["lineItems"])


async def test_a_new_customer_is_created_and_attached(shopify):
    """Otherwise the order is filed under "No customer" and the shop cannot see that
    this person has ordered before."""
    await service.create_cod_order(_items(), **GOOD)

    created = shopify.created_customers[0]
    assert created == {"firstName": "Mona", "lastName": "Hassan",
                       "phone": "+201067177129"}
    assert shopify.created[0]["order"]["customer"] == {
        "toAssociate": {"id": "gid://shopify/Customer/999"}}


async def test_a_returning_customer_is_reused_not_duplicated(shopify):
    shopify.customers = [{"id": "gid://shopify/Customer/1",
                          "defaultPhoneNumber": {"phoneNumber": "+201067177129"},
                          "defaultEmailAddress": None}]

    await service.create_cod_order(_items(), **GOOD)

    assert shopify.created_customers == []
    assert shopify.created[0]["order"]["customer"]["toAssociate"]["id"] \
        == "gid://shopify/Customer/1"


async def test_a_loosely_matched_customer_is_not_reused(shopify):
    """Shopify's customer search matches as loosely as its order search, so attaching
    the first row would file the order under a different person entirely."""
    shopify.customers = [{"id": "gid://shopify/Customer/2",
                          "defaultPhoneNumber": {"phoneNumber": "+201000000000"},
                          "defaultEmailAddress": None}]

    await service.create_cod_order(_items(), **GOOD)

    # A new record, because the returned one was somebody else.
    assert shopify.created_customers
    assert shopify.created[0]["order"]["customer"]["toAssociate"]["id"] \
        == "gid://shopify/Customer/999"


async def test_an_email_is_added_to_the_customer_when_given(shopify):
    await service.create_cod_order(_items(), email="mona@example.com", **GOOD)
    assert shopify.created_customers[0]["email"] == "mona@example.com"


async def test_a_customer_failure_never_costs_the_order(shopify):
    """An order with nobody attached is still an order; a lost sale is not."""
    from app.integrations.shopify.client import ShopifyRejected

    shopify.customer_error = ShopifyRejected("Phone has already been taken")
    await service.create_cod_order(_items(), **GOOD)

    assert len(shopify.created) == 1
    assert "customer" not in shopify.created[0]["order"]


def _methods(shopify):
    zone = shopify.profiles[0]["profileLocationGroups"][0]["locationGroupZones"]["nodes"][0]
    return zone["methodDefinitions"]["nodes"]


async def test_the_stores_own_delivery_rate_is_charged(shopify):
    """The amount is cash a courier collects, so it has to be the shop's real rate."""
    await service.create_cod_order(_items(), **GOOD)
    line = shopify.created[0]["order"]["shippingLines"][0]

    assert line["title"] == "Standard"
    assert line["priceSet"]["shopMoney"] == {"amount": "118.0", "currencyCode": "EGP"}


async def test_nothing_is_charged_when_no_rate_exists_and_none_is_configured(shopify):
    """A made-up delivery charge changes what the courier collects."""
    shopify.profiles = []
    await service.create_cod_order(_items(), **GOOD)
    assert "shippingLines" not in shopify.created[0]["order"]


async def test_the_configured_fee_is_only_a_fallback(shopify, monkeypatch):
    """For when read_shipping is missing or Shopify will not answer."""
    from app.integrations.shopify.client import ShopifyAuthError

    shopify.rates_error = ShopifyAuthError("Access denied for deliveryProfiles field")
    monkeypatch.setattr(service.settings, "cod_shipping_fee", 60.0, raising=False)

    await service.create_cod_order(_items(), **GOOD)
    line = shopify.created[0]["order"]["shippingLines"][0]
    assert line["priceSet"]["shopMoney"]["amount"] == "60.0"


async def test_a_zone_that_excludes_the_province_is_not_charged(shopify):
    """Charging a rate for a governorate its zone does not cover would be wrong."""
    details = dict(GOOD, city="Nowhere")
    await service.create_cod_order(_items(), governorate="Aswan", **details)
    assert "shippingLines" not in shopify.created[0]["order"]


async def test_the_cheapest_applicable_rate_wins(shopify):
    """The customer never asked for express, so they must not be billed for it."""
    _methods(shopify).append({
        "name": "Express", "active": True,
        "rateProvider": {"__typename": "DeliveryRateDefinition",
                         "price": {"amount": "250.0", "currencyCode": "EGP"}},
        "methodConditions": [],
    })
    await service.create_cod_order(_items(), **GOOD)
    assert shopify.created[0]["order"]["shippingLines"][0]["title"] == "Standard"


async def test_an_inactive_rate_is_ignored(shopify):
    _methods(shopify)[0]["active"] = False
    await service.create_cod_order(_items(), **GOOD)
    assert "shippingLines" not in shopify.created[0]["order"]


async def test_a_carrier_calculated_rate_is_skipped_not_guessed(shopify):
    """It needs a live quote from the carrier, which a chat order cannot get."""
    _methods(shopify)[0]["rateProvider"] = {"__typename": "DeliveryParticipant"}
    await service.create_cod_order(_items(), **GOOD)
    assert "shippingLines" not in shopify.created[0]["order"]


async def test_a_free_over_threshold_rate_is_honoured(shopify):
    """Otherwise a customer who qualified for free delivery is charged anyway."""
    _methods(shopify)[:] = [
        {"name": "Standard", "active": True,
         "rateProvider": {"__typename": "DeliveryRateDefinition",
                          "price": {"amount": "118.0", "currencyCode": "EGP"}},
         "methodConditions": [{"field": "TOTAL_PRICE",
                               "operator": "LESS_THAN_OR_EQUAL_TO",
                               "conditionCriteria": {"__typename": "MoneyV2",
                                                     "amount": "500.0"}}]},
        {"name": "Free over 500", "active": True,
         "rateProvider": {"__typename": "DeliveryRateDefinition",
                          "price": {"amount": "0.0", "currencyCode": "EGP"}},
         "methodConditions": [{"field": "TOTAL_PRICE",
                               "operator": "GREATER_THAN_OR_EQUAL_TO",
                               "conditionCriteria": {"__typename": "MoneyV2",
                                                     "amount": "500.0"}}]},
    ]
    # One 580 EGP tee, so the 118 rate no longer applies and free delivery does.
    await service.create_cod_order(_items(), **GOOD)
    line = shopify.created[0]["order"]["shippingLines"][0]

    assert line["title"] == "Free over 500"
    assert line["priceSet"]["shopMoney"]["amount"] == "0.0"


async def test_the_prompt_asks_for_the_governorate_separately(shopify):
    """The order that failed had the governorate typed into the city box."""
    from app.modules.chat import agent

    prompt = agent.build_system_prompt()
    assert "governorate (المحافظة) is a separate thing from the city" in prompt


async def test_the_tool_requires_a_governorate(shopify):
    declaration = next(d for d in tools.declarations()
                       if d["name"] == "create_cod_order")
    assert "governorate" in declaration["parameters"]["required"]


# --- quoting delivery before the customer commits ------------------------
#
# Asked how much delivery costs, the bot answered that it had no information: the rate
# was only ever read inside order creation, long after the customer needed to know it.


async def test_the_delivery_cost_can_be_quoted(shopify):
    result = await tools.dispatch("get_delivery_cost", {"governorate": "Cairo"})

    assert result["available"] is True
    assert result["cost"] == "118 EGP"
    assert result["method"] == "Standard"


async def test_a_district_is_enough_to_quote(shopify):
    """Customers write "القاهرة - المعادي - شارع ٩٠", not a governorate."""
    result = await tools.dispatch("get_delivery_cost", {"governorate": "المعادي"})
    assert result["cost"] == "118 EGP"


async def test_delivery_is_quotable_before_an_address_is_given(shopify):
    """Every zone here charges the same, so the destination does not change the answer."""
    result = await tools.dispatch("get_delivery_cost", {})
    assert result["available"] is True


async def test_no_quote_is_given_when_the_rate_depends_on_an_unknown_destination(shopify):
    """Naming one of two different rates would be a guess at what the courier collects."""
    _methods(shopify).append({
        "name": "Remote areas", "active": True,
        "rateProvider": {"__typename": "DeliveryRateDefinition",
                         "price": {"amount": "250.0", "currencyCode": "EGP"}},
        "methodConditions": [],
    })
    zone = shopify.profiles[0]["profileLocationGroups"][0]["locationGroupZones"]["nodes"][0]
    zone["zone"]["countries"][0]["provinces"] = [{"code": "C"}]

    result = await tools.dispatch("get_delivery_cost", {})
    assert result["available"] is False


async def test_an_unquotable_destination_says_so_rather_than_naming_a_figure(shopify):
    shopify.profiles = []
    result = await tools.dispatch("get_delivery_cost", {"governorate": "Cairo"})

    assert result["available"] is False
    assert result["error"] == "no_rate_for_this_destination"


async def test_the_delivery_quote_carries_data_only(shopify):
    shopify.profiles = []
    result = await tools.dispatch("get_delivery_cost", {})
    flattened = str(result).lower()
    for directive in ("tell the customer", "you should", "ask the customer", "instruction"):
        assert directive not in flattened, result


async def test_an_order_reports_the_delivery_it_was_charged(shopify):
    """So the bot can say "580 for the tee plus 118 delivery" rather than one figure."""
    order = await service.create_cod_order(_items(), **GOOD)
    payload = order.to_tool_dict()

    assert payload["delivery"] == "118 EGP"
    assert payload["items_total"] == "580 EGP"


async def test_the_finished_totals_are_handed_over_so_nothing_is_computed(shopify):
    """Live fault: the tool returned 118 EGP and the bot told the customer 50 EGP.

    It quoted 118 correctly a turn later, so the path was fine and the model was not.
    Handing over the finished strings leaves it nothing to add up or half-remember.
    """
    result = await tools.dispatch("get_delivery_cost",
                                  {"governorate": "Cairo", "order_total": 580})

    assert result["cost"] == "118 EGP"
    assert result["items_total"] == "580 EGP"
    assert result["total_with_delivery"] == "698 EGP"


async def test_the_prompt_forbids_a_remembered_shipping_price(shopify):
    from app.modules.chat import agent

    prompt = agent.build_system_prompt().replace("\n", " ")
    assert "get_delivery_cost" in prompt
    assert "is not evidence and must never appear in a reply" in prompt


async def test_the_prompt_forbids_bullet_characters(shopify):
    """It answered with a "- item" list, which the widget renders literally."""
    from app.modules.chat import agent

    prompt = agent.build_system_prompt().replace("\n", " ")
    assert 'Never begin a line with "-"' in prompt
