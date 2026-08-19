"""Identifying a product from a photo, and the checks that decide whether to assert it.

The store owner chose "assert when confident, ask otherwise", which puts all the weight
on what `confident` means. It is deliberately not the model's own word: these tests pin
down each condition that can withhold it, because every one of them corresponds to a way
the bot could otherwise tell a customer something false with total assurance.
"""

import json

import pytest

from app.integrations.llm_types import ImagePart, LLMResponse, LLMUnavailable
from app.modules.catalog import service
from app.modules.catalog.service import CatalogUnavailable
from app.modules.chat import tools

PHOTO = [ImagePart(data=b"\xff\xd8\xff\xe0" + b"\x00" * 200, mime_type="image/jpeg")]


def _variant(size, color, available=True):
    return {
        "id": "v-" + size + color, "title": size + " / " + color, "sku": None,
        "price": "550.00", "availableForSale": available, "inventoryQuantity": 5,
        "selectedOptions": [{"name": "Size", "value": size},
                            {"name": "Color", "value": color}],
    }


def _node(title, product_type="T-Shirts", variants=None):
    return {
        "id": "gid://shopify/Product/" + title.replace(" ", ""),
        "title": title, "handle": title.lower().replace(" ", "-"),
        "productType": product_type, "tags": [], "status": "ACTIVE",
        "onlineStoreUrl": None, "totalInventory": 10, "description": "",
        "featuredImage": None,
        "priceRangeV2": {"minVariantPrice": {"amount": "550.00", "currencyCode": "EGP"},
                         "maxVariantPrice": {"amount": "550.00", "currencyCode": "EGP"}},
        "variants": {"nodes": variants or [_variant("M", "Brown"), _variant("L", "Navy")]},
    }


CATALOG = [
    _node("RINGER BOXY FIT TSHIRT"),
    _node("BOXY WNS TEE", variants=[_variant("M", "Black"), _variant("L", "White")]),
    _node("WANAS HOODIE", product_type="Hoodies & Sweatshirts",
          variants=[_variant("M", "Olive")]),
]


class FakeShopify:
    def __init__(self, nodes=None, error=None):
        self.nodes = nodes if nodes is not None else CATALOG
        self.error = error

    async def fetch_all_products(self, **_kwargs):
        if self.error:
            raise self.error
        return self.nodes


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    service.clear_cache()
    fake = FakeShopify()
    monkeypatch.setattr(service, "_shopify", lambda: fake)
    yield fake
    service.clear_cache()


@pytest.fixture
def identifier(monkeypatch):
    """Script what the identifying model replies, and record what it was asked."""
    state = {}

    def script(payload, *, raises=None):
        async def generate(*, turns, system_instruction=None, tools=None,
                           temperature=None, json_output=False, model=None):
            state["turns"] = list(turns)
            state["json_output"] = json_output
            state["system_instruction"] = system_instruction
            if raises:
                raise raises
            text = payload if isinstance(payload, str) else json.dumps(payload)
            return LLMResponse(text=text, model="test-model", provider="test")

        monkeypatch.setattr(service.llm, "generate", generate)
        return state

    return script


def _candidate(title="RINGER BOXY FIT TSHIRT", color="Brown", confidence="high",
               evidence="brown body with contrast collar"):
    return {"title": title, "color": color, "confidence": confidence,
            "evidence": evidence}


def _reply(*candidates, description="a brown ringer t-shirt"):
    return {"description": description, "candidates": list(candidates)}


# --- what earns confidence -----------------------------------------------


async def test_a_checked_high_confidence_match_is_asserted(identifier):
    identifier(_reply(_candidate()))
    found = await service.identify_product_from_image(PHOTO)

    assert found.confident is True
    assert found.matches[0].product.title == "RINGER BOXY FIT TSHIRT"
    assert found.matches[0].color == "Brown"


async def test_the_model_only_sees_the_real_catalog(identifier):
    """It must choose from what the store actually sells, not from memory."""
    state = identifier(_reply(_candidate()))
    await service.identify_product_from_image(PHOTO)

    prompt = state["turns"][0].text
    assert "RINGER BOXY FIT TSHIRT" in prompt
    assert "WANAS HOODIE" in prompt
    assert state["json_output"] is True
    # The photo itself must actually be attached, not just described.
    assert state["turns"][0].images == PHOTO


# --- what withholds it ---------------------------------------------------


@pytest.mark.parametrize("confidence", ["medium", "low", "", "very high", "unsure"])
async def test_anything_short_of_high_is_not_asserted(identifier, confidence):
    identifier(_reply(_candidate(confidence=confidence)))
    found = await service.identify_product_from_image(PHOTO)

    assert found.confident is False
    # The candidate is still offered - the customer can confirm it themselves.
    assert len(found.matches) == 1


async def test_two_equally_good_matches_are_never_asserted(identifier):
    """A plain garment fits several products; picking one would be a coin toss."""
    identifier(_reply(_candidate(), _candidate(title="BOXY WNS TEE", color="Black")))
    found = await service.identify_product_from_image(PHOTO)

    assert found.confident is False
    assert [match.product.title for match in found.matches] == [
        "RINGER BOXY FIT TSHIRT", "BOXY WNS TEE"]
    assert "equally" in found.reason


async def test_a_second_weaker_candidate_does_not_block_the_first(identifier):
    identifier(_reply(_candidate(),
                      _candidate(title="BOXY WNS TEE", color="Black", confidence="low")))
    found = await service.identify_product_from_image(PHOTO)

    assert found.confident is True


async def test_a_colour_the_product_is_not_made_in_withholds_confidence(identifier):
    """If the photo is pink and this piece has never been pink, it is not this piece."""
    identifier(_reply(_candidate(color="Pink")))
    found = await service.identify_product_from_image(PHOTO)

    assert found.confident is False
    assert found.matches[0].color is None
    assert "colour" in found.reason


async def test_a_product_that_does_not_exist_is_dropped(identifier):
    """Live risk: the model naming a plausible-sounding piece the store never sold."""
    identifier(_reply(_candidate(title="WANAS VARSITY JACKET"), _candidate()))
    found = await service.identify_product_from_image(PHOTO)

    assert [match.product.title for match in found.matches] == ["RINGER BOXY FIT TSHIRT"]
    assert found.confident is True


async def test_an_entirely_invented_answer_becomes_no_match_at_all(identifier):
    identifier(_reply(_candidate(title="SOMETHING ELSE ENTIRELY")))
    found = await service.identify_product_from_image(PHOTO)

    assert found.matches == []
    assert found.confident is False


async def test_no_candidates_is_a_valid_answer(identifier):
    """A photo of another brand's piece should match nothing, and that must be sayable."""
    identifier(_reply(description="a red leather handbag"))
    found = await service.identify_product_from_image(PHOTO)

    assert found.matches == []
    assert found.confident is False
    assert found.description == "a red leather handbag"


# --- when the identifier itself misbehaves --------------------------------


async def test_a_model_failure_is_an_uncertain_answer_not_an_exception(identifier):
    """"I could not tell" is a perfectly good thing to say to a customer."""
    identifier(None, raises=LLMUnavailable("503"))
    found = await service.identify_product_from_image(PHOTO)

    assert found.confident is False
    assert found.matches == []


@pytest.mark.parametrize("garbage", ["not json at all", "", "[]", "null"])
async def test_unparseable_output_is_handled(identifier, garbage):
    identifier(garbage)
    found = await service.identify_product_from_image(PHOTO)
    assert found.confident is False


async def test_json_wrapped_in_a_code_fence_is_still_read(identifier):
    identifier("```json\n" + json.dumps(_reply(_candidate())) + "\n```")
    found = await service.identify_product_from_image(PHOTO)
    assert found.confident is True


async def test_a_broken_candidate_entry_does_not_break_the_rest(identifier):
    identifier({"description": "x", "candidates": ["not an object", _candidate()]})
    found = await service.identify_product_from_image(PHOTO)
    assert [m.product.title for m in found.matches] == ["RINGER BOXY FIT TSHIRT"]


async def test_no_image_means_no_call_to_the_model(identifier):
    state = identifier(_reply(_candidate()))
    found = await service.identify_product_from_image([])

    assert found.matches == []
    assert "turns" not in state


async def test_an_unreadable_catalog_is_raised_not_guessed_around(catalog, identifier):
    """Matching against an empty catalog would mean matching against nothing."""
    from app.integrations.shopify.client import ShopifyUnavailable

    identifier(_reply(_candidate()))
    catalog.error = ShopifyUnavailable("503")
    with pytest.raises(CatalogUnavailable):
        await service.identify_product_from_image(PHOTO)


# --- through the tools layer ---------------------------------------------


async def test_the_tool_takes_no_arguments_and_reads_the_current_photo():
    """The photo cannot be an argument, so it travels as dispatch context instead."""
    declaration = next(d for d in tools.declarations()
                       if d["name"] == "identify_product_from_image")
    assert declaration["parameters"]["properties"] == {}


async def test_the_tool_passes_the_message_images_through(identifier):
    identifier(_reply(_candidate()))
    result = await tools.dispatch("identify_product_from_image", {},
                                    context=tools.ToolContext(images=PHOTO))

    assert result["confident"] is True
    assert result["matches"][0]["title"] == "RINGER BOXY FIT TSHIRT"
    assert result["matches"][0]["matched_color"] == "Brown"


async def test_calling_it_without_a_photo_is_reported_as_data(identifier):
    """Happens when a customer refers back to a picture the model can no longer see."""
    identifier(_reply(_candidate()))
    result = await tools.dispatch("identify_product_from_image", {},
                                    context=tools.ToolContext(images=[]))

    assert result == {"error": "no_image_in_this_message"}


async def test_other_tools_are_not_handed_the_photo(identifier, monkeypatch):
    """Only tools that ask for images receive them."""
    seen = {}

    async def fake_search(query, limit=5):
        seen["called"] = True
        return []

    monkeypatch.setattr(tools.catalog_service, "search_products", fake_search)
    # Would raise TypeError if dispatch passed images to a handler that takes none.
    await tools.dispatch("search_products", {"query": "tee"},
                         context=tools.ToolContext(images=PHOTO))
    assert seen["called"] is True


async def test_the_result_carries_data_only_never_instructions(identifier):
    """The same rule as every other tool: a directive in the payload gets read aloud."""
    identifier(_reply(_candidate(confidence="low")))
    result = await tools.dispatch("identify_product_from_image", {},
                                    context=tools.ToolContext(images=PHOTO))

    flattened = str(result).lower()
    for directive in ("tell the customer", "you should", "ask the customer",
                      "instruction", "not sure", "withheld"):
        assert directive not in flattened, result
