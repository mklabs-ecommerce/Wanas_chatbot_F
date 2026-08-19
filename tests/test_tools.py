"""The tools layer: declarations, dispatch, and the agent's tool-calling loop."""

import pytest

from app.integrations.llm_types import LLMResponse, ToolCall
from app.modules.catalog.schemas import Product, Variant
from app.modules.catalog.service import CatalogUnavailable
from app.modules.chat import agent, repository, tools
from app.modules.orders.schemas import Order
from app.modules.orders.service import OrdersUnavailable


def _product(title="Cairokee T-shirt", available=True):
    return Product(
        id="gid://shopify/Product/1", title=title, handle="cairokee-t-shirt",
        product_type="T-Shirts", price_min="600.0", price_max="600.0", currency="EGP",
        variants=[Variant(id="v1", title="M / Black", price="600.0", currency="EGP",
                          available=available, options={"Size": "M", "Color": "Black"})],
    )


# --- declarations --------------------------------------------------------


def test_declarations_are_plain_json_schema():
    """Gemini accepts this shape unchanged, with no SDK-specific types."""
    declared = tools.declarations()
    assert [d["name"] for d in declared] == [
        "search_products", "browse_products", "get_order_status",
        "get_orders_by_customer", "get_delivery_cost", "identify_product_from_image",
        "create_cod_order", "create_support_ticket", "record_feedback",
    ]
    schema = declared[0]["parameters"]
    assert schema["type"] == "object"
    assert schema["required"] == ["query"]
    assert schema["properties"]["query"]["type"] == "string"


def test_only_the_built_tools_exist_at_this_build_step():
    """Step 7 (draft orders, online payment) is still deferred - nothing for it yet."""
    assert tools.names() == ["search_products", "browse_products", "get_order_status",
                             "get_orders_by_customer", "get_delivery_cost",
                             "identify_product_from_image", "create_cod_order",
                             "create_support_ticket", "record_feedback"]


# --- dispatch -----------------------------------------------------------


async def test_search_dispatches_to_the_catalog_service(monkeypatch):
    seen = {}

    async def fake_search(query, limit=5):
        seen["query"] = query
        seen["limit"] = limit
        return [_product()]

    monkeypatch.setattr(tools.catalog_service, "search_products", fake_search)
    result = await tools.dispatch("search_products", {"query": "black tee", "limit": 2})

    assert seen == {"query": "black tee", "limit": 2}
    assert result["count"] == 1
    assert result["products"][0]["title"] == "Cairokee T-shirt"
    assert result["products"][0]["price"] == "600 EGP"


async def test_limit_is_clamped_to_the_maximum(monkeypatch):
    seen = {}

    async def fake_search(query, limit=5):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(tools.catalog_service, "search_products", fake_search)
    await tools.dispatch("search_products", {"query": "tee", "limit": 99})
    assert seen["limit"] == tools.MAX_SEARCH_RESULTS


async def test_nonsense_limit_falls_back_to_the_default(monkeypatch):
    seen = {}

    async def fake_search(query, limit=5):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(tools.catalog_service, "search_products", fake_search)
    await tools.dispatch("search_products", {"query": "tee", "limit": "lots"})
    assert seen["limit"] == tools.MAX_SEARCH_RESULTS


async def test_blank_query_is_reported_not_searched(monkeypatch):
    async def fake_search(query, limit=5):
        raise AssertionError("should not be called")

    monkeypatch.setattr(tools.catalog_service, "search_products", fake_search)
    assert "error" in await tools.dispatch("search_products", {"query": "  "})


async def test_no_results_returns_an_empty_list_not_an_error(monkeypatch):
    async def fake_search(query, limit=5):
        return []

    monkeypatch.setattr(tools.catalog_service, "search_products", fake_search)
    result = await tools.dispatch("search_products", {"query": "leather bag"})
    assert result == {"query": "leather bag", "count": 0, "products": []}


async def test_tool_results_never_contain_instructions(monkeypatch):
    """Regression: text phrased as a directive inside a tool result gets read aloud.

    The model once replied "I should inform the customer about this and offer to look for
    something else." because that sentence was in the payload. Results carry data only.
    """
    async def empty(query, limit=5):
        return []

    async def broken(query, limit=5):
        raise CatalogUnavailable("503")

    for fake in (empty, broken):
        monkeypatch.setattr(tools.catalog_service, "search_products", fake)
        result = await tools.dispatch("search_products", {"query": "tee"})
        flattened = str(result).lower()
        for directive in ("tell the customer", "say ", "instruction", "you should", "do not"):
            assert directive not in flattened, result


async def test_catalog_outage_is_reported_as_data(monkeypatch):
    async def fake_search(query, limit=5):
        raise CatalogUnavailable("Shopify is down")

    monkeypatch.setattr(tools.catalog_service, "search_products", fake_search)
    assert await tools.dispatch("search_products", {"query": "tee"}) == {
        "error": "catalog_unavailable"
    }


async def test_unknown_tool_name_is_reported_not_raised():
    """A hallucinated tool name must let the model correct itself, not kill the turn."""
    result = await tools.dispatch("cancel_order", {})
    assert "error" in result


async def test_a_crashing_tool_does_not_break_the_conversation(monkeypatch):
    async def explodes(query, limit=5):
        raise RuntimeError("boom")

    monkeypatch.setattr(tools.catalog_service, "search_products", explodes)
    result = await tools.dispatch("search_products", {"query": "tee"})
    assert "error" in result


# --- the agent's tool loop ----------------------------------------------


@pytest.fixture
def scripted_llm(monkeypatch):
    """Drive the agent with a scripted sequence of model responses."""
    state = {"calls": 0, "sent": []}

    def script(*responses):
        async def generate(*, turns, system_instruction=None, tools=None, temperature=None,
                       json_output=False, model=None):
            state["sent"].append(list(turns))
            state["tools_offered"] = tools
            index = min(state["calls"], len(responses) - 1)
            state["calls"] += 1
            return responses[index]

        monkeypatch.setattr(agent.llm, "generate", generate)
        return state

    return script


async def test_tool_call_is_executed_and_its_result_fed_back(scripted_llm, monkeypatch):
    async def fake_search(query, limit=5):
        return [_product()]

    monkeypatch.setattr(tools.catalog_service, "search_products", fake_search)
    state = scripted_llm(
        LLMResponse(text="", tool_calls=[ToolCall(name="search_products",
                                                 arguments={"query": "tee"})],
                    model="m", provider="gemini"),
        LLMResponse(text="The Cairokee T-shirt is 600 EGP.", model="m", provider="gemini"),
    )

    reply = await agent.handle_message("what tees do you have?")

    assert reply.text == "The Cairokee T-shirt is 600 EGP."
    assert reply.tools_used == ["search_products"]
    assert state["calls"] == 2

    # The second request replayed the call and its result.
    second = state["sent"][1]
    assert second[-2].tool_calls[0].name == "search_products"
    assert second[-1].tool_results[0].result["count"] == 1


async def test_tools_are_offered_to_the_model(scripted_llm):
    state = scripted_llm(LLMResponse(text="hello", model="m", provider="gemini"))
    await agent.handle_message("hello")
    assert [t["name"] for t in state["tools_offered"]] == tools.names()


async def test_only_the_final_text_is_stored_in_history(scripted_llm, monkeypatch):
    """Tool JSON must not accumulate in the conversation record."""
    async def fake_search(query, limit=5):
        return [_product()]

    monkeypatch.setattr(tools.catalog_service, "search_products", fake_search)
    scripted_llm(
        LLMResponse(text="", tool_calls=[ToolCall(name="search_products", arguments={"query": "t"})],
                    model="m", provider="gemini"),
        LLMResponse(text="Here it is.", model="m", provider="gemini"),
    )

    reply = await agent.handle_message("tees?")
    history = repository.get_recent_messages(reply.conversation_id, limit=10)
    assert [(row["role"], row["content"]) for row in history] == [
        ("user", "tees?"), ("model", "Here it is."),
    ]


async def test_a_model_that_only_ever_calls_tools_is_stopped_at_the_round_limit(
    scripted_llm, monkeypatch
):
    """Without a cap, a looping model would drain the free-tier quota."""
    async def fake_search(query, limit=5):
        return []

    monkeypatch.setattr(tools.catalog_service, "search_products", fake_search)
    responses = [
        LLMResponse(text="", tool_calls=[ToolCall(name="search_products", arguments={"query": "t"})],
                    model="m", provider="gemini")
    ] * agent.settings.max_tool_rounds
    responses.append(LLMResponse(text="I could not narrow that down.", model="m", provider="gemini"))
    state = scripted_llm(*responses)

    reply = await agent.handle_message("tees?")
    # One request per round, plus a final tool-free request that forces a worded answer.
    assert state["calls"] == agent.settings.max_tool_rounds + 1
    assert state["tools_offered"] is None, "the final request must offer no tools"
    assert reply.text == "I could not narrow that down."


async def test_system_prompt_forbids_markdown_and_narration():
    prompt = agent.build_system_prompt()
    assert "Plain text only" in prompt
    assert "Never narrate your own reasoning" in prompt
    assert "search_products" in prompt


# --- browse_products dispatch -------------------------------------------


async def test_browse_is_registered_alongside_search():
    assert "browse_products" in tools.names()
    declaration = tools.declarations()[1]
    assert declaration["name"] == "browse_products"
    # No required fields: asking for the cheapest item needs no arguments at all.
    assert "required" not in declaration["parameters"]
    assert declaration["parameters"]["properties"]["sort"]["enum"] == ["price_asc", "price_desc"]


async def test_browse_passes_filters_through(monkeypatch):
    seen = {}

    async def fake_browse(category=None, max_price=None, min_price=None,
                          sort="price_asc", limit=8):
        seen.update(category=category, max_price=max_price, min_price=min_price,
                    sort=sort, limit=limit)
        return [_product()]

    monkeypatch.setattr(tools.catalog_service, "browse_products", fake_browse)
    result = await tools.dispatch("browse_products", {
        "category": "hoodies", "max_price": "700", "sort": "price_desc", "limit": 3,
    })

    assert seen == {"category": "hoodies", "max_price": 700.0, "min_price": None,
                    "sort": "price_desc", "limit": 3}
    assert result["count"] == 1


async def test_browse_ignores_a_bad_sort_value(monkeypatch):
    seen = {}

    async def fake_browse(category=None, max_price=None, min_price=None,
                          sort="price_asc", limit=8):
        seen["sort"] = sort
        return []

    monkeypatch.setattr(tools.catalog_service, "browse_products", fake_browse)
    await tools.dispatch("browse_products", {"sort": "cheapest please"})
    assert seen["sort"] == "price_asc"


async def test_browse_ignores_an_unparseable_price(monkeypatch):
    seen = {}

    async def fake_browse(category=None, max_price=None, min_price=None,
                          sort="price_asc", limit=8):
        seen["max_price"] = max_price
        return []

    monkeypatch.setattr(tools.catalog_service, "browse_products", fake_browse)
    await tools.dispatch("browse_products", {"max_price": "cheap"})
    assert seen["max_price"] is None


async def test_browse_outage_is_reported_as_data(monkeypatch):
    async def broken(**_kwargs):
        raise CatalogUnavailable("503")

    monkeypatch.setattr(tools.catalog_service, "browse_products", broken)
    assert await tools.dispatch("browse_products", {}) == {"error": "catalog_unavailable"}


async def test_the_prompt_forbids_recombining_colours_and_sizes():
    """Live fault: "Brown, Navy, Beige, Burgundy in M, L and XL" when Burgundy is XL only."""
    prompt = agent.build_system_prompt()
    assert "does not exist in \\\nanother" in prompt or "does not exist in" in prompt
    assert "give that colour's price, not the product's range" in prompt


async def test_prompt_tells_the_model_which_catalog_tool_to_use():
    prompt = agent.build_system_prompt()
    assert "Use search_products when the customer names a garment" in prompt
    assert "Only browse_products sees the entire catalog" in prompt


# --- order lookup dispatch ----------------------------------------------


def _order(number="#1003"):
    return Order(id="gid://shopify/Order/1", number=number, placed_on="2026-08-16",
                 financial_status="PENDING", fulfillment_status="UNFULFILLED",
                 total="650", currency="EGP", phone="+201067177129")


async def test_order_status_requires_both_arguments():
    """The contact is what proves the order belongs to whoever is asking."""
    declaration = next(d for d in tools.declarations() if d["name"] == "get_order_status")
    assert declaration["parameters"]["required"] == ["order_number", "contact"]


async def test_order_status_passes_the_contact_to_the_service(monkeypatch):
    seen = {}

    async def fake(order_number, contact=None):
        seen.update(order_number=order_number, contact=contact)
        return _order()

    monkeypatch.setattr(tools.orders_service, "get_order_status", fake)
    result = await tools.dispatch("get_order_status",
                                  {"order_number": "1003", "contact": "01067177129"})

    assert seen == {"order_number": "1003", "contact": "01067177129"}
    assert result["found"] is True
    assert result["order"]["order_number"] == "#1003"


async def test_a_refused_lookup_looks_exactly_like_a_missing_order(monkeypatch):
    """Identical results, so the tool cannot be used to find real order numbers."""
    async def not_found(order_number, contact=None):
        return None

    monkeypatch.setattr(tools.orders_service, "get_order_status", not_found)
    missing = await tools.dispatch("get_order_status",
                                   {"order_number": "9999", "contact": "01067177129"})
    refused = await tools.dispatch("get_order_status",
                                   {"order_number": "9999", "contact": "wrong@example.com"})

    assert missing == refused
    assert missing["found"] is False


async def test_order_lookup_without_a_number_never_reaches_the_service(monkeypatch):
    async def boom(order_number, contact=None):
        raise AssertionError("should not have been called")

    monkeypatch.setattr(tools.orders_service, "get_order_status", boom)
    assert "error" in await tools.dispatch("get_order_status", {"contact": "x@y.com"})


async def test_orders_by_customer_drops_line_items(monkeypatch):
    """Five orders' worth of items would crowd out everything else in the context."""
    async def fake(contact, limit=5):
        return [_order("#1003"), _order("#1000")]

    monkeypatch.setattr(tools.orders_service, "get_orders_by_customer", fake)
    result = await tools.dispatch("get_orders_by_customer", {"contact": "01067177129"})

    assert result["count"] == 2
    assert all("items" not in order for order in result["orders"])


async def test_orders_outage_is_reported_as_data(monkeypatch):
    async def fake(contact, limit=5):
        raise OrdersUnavailable("Shopify is down")

    monkeypatch.setattr(tools.orders_service, "get_orders_by_customer", fake)
    assert await tools.dispatch("get_orders_by_customer", {"contact": "x@y.com"}) == {
        "error": "orders_unavailable"
    }


async def test_order_tool_results_never_contain_instructions(monkeypatch):
    """The same rule as the catalog tools: data only, never a directive."""
    async def not_found(order_number, contact=None):
        return None

    async def broken(contact, limit=5):
        raise OrdersUnavailable("503")

    monkeypatch.setattr(tools.orders_service, "get_order_status", not_found)
    monkeypatch.setattr(tools.orders_service, "get_orders_by_customer", broken)

    results = [
        await tools.dispatch("get_order_status", {"order_number": "1", "contact": "a@b.com"}),
        await tools.dispatch("get_orders_by_customer", {"contact": "a@b.com"}),
    ]
    for result in results:
        flattened = str(result).lower()
        for directive in ("tell the customer", "say ", "instruction", "you should", "ask them"):
            assert directive not in flattened, result


async def test_prompt_tells_the_model_to_verify_before_looking_up_an_order():
    prompt = agent.build_system_prompt()
    assert "get_order_status" in prompt
    assert "get_orders_by_customer" in prompt
    # The limitation line must have been lifted now that the tools exist.
    assert "cannot look up, create or modify orders" not in prompt
    # Live finding: asked in Arabic, the bot reported "not shipped yet" as "being
    # prepared" - a small reassurance it had no grounds for.
    assert "do not upgrade it" in prompt
