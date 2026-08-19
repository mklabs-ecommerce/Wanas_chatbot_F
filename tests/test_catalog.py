"""Catalog module: Shopify payload conversion, relevance ranking and caching.

Uses recorded payloads shaped like the real store's GraphQL response, so the suite never
calls Shopify.
"""

import pytest

from app.integrations.shopify.client import ShopifyUnavailable
from app.modules.catalog import service
from app.modules.catalog.schemas import Product, Variant


def _variant(vid, size=None, color=None, price="600.00", available=True, inventory=10):
    options = []
    if size is not None:
        options.append({"name": "Size", "value": size})
    if color is not None:
        options.append({"name": "Color", "value": color})
    return {
        "id": vid,
        "title": " / ".join(v for v in (size, color) if v),
        "sku": vid.lower(),
        "price": price,
        "availableForSale": available,
        "inventoryQuantity": inventory,
        "selectedOptions": options,
    }


def _node(title, product_type="T-Shirts", status="ACTIVE", tags=None, variants=None,
          description="", price=("600.00", "600.00"), online_url=None, handle=None):
    return {
        "id": "gid://shopify/Product/" + title.replace(" ", ""),
        "title": title,
        "handle": handle or title.lower().replace(" ", "-"),
        "productType": product_type,
        "tags": tags or [],
        "status": status,
        "onlineStoreUrl": online_url,
        "totalInventory": 10,
        "description": description,
        "featuredImage": {"url": "https://cdn.example/img.jpg", "altText": None},
        "priceRangeV2": {
            "minVariantPrice": {"amount": price[0], "currencyCode": "EGP"},
            "maxVariantPrice": {"amount": price[1], "currencyCode": "EGP"},
        },
        "variants": {"nodes": variants if variants is not None else [_variant("V1", "M", "Black")]},
    }


CATALOG_NODES = [
    _node("Cairokee T-shirt", tags=["Cairokee Merch", "T-Shirts"],
          variants=[_variant("V1", "S", "Brown"), _variant("V2", "S", "Black"),
                    _variant("V3", "M", "Black")]),
    _node("Cairokee Hoodie", product_type="Hoodies & Sweatshirts", price=("800.00", "800.00"),
          variants=[_variant("V4", "S", "Black", price="800.00"),
                    _variant("V5", "M", "Brown", price="800.00", available=False, inventory=0)]),
    _node("WANAS HOODIE", product_type="Hoodies & Sweatshirts", price=("650.00", "700.00"),
          variants=[_variant("V6", "L", "Olive", price="650.00")]),
    _node("BOXY WNS TEE", description="Oversized fit, dropped shoulder.",
          price=("590.00", "590.00"),
          variants=[_variant("V7", "M", "Olive", price="590.00")]),
    _node("Secret Sample", status="DRAFT"),
    _node("Old Season", status="ARCHIVED"),
]


class FakeShopify:
    """Stands in for ShopifyClient; can be told to fail."""

    def __init__(self, nodes=None, error=None):
        self.nodes = nodes if nodes is not None else CATALOG_NODES
        self.error = error
        self.fetches = 0

    async def fetch_all_products(self, **_kwargs):
        self.fetches += 1
        if self.error:
            raise self.error
        return self.nodes


@pytest.fixture(autouse=True)
def fresh_catalog(monkeypatch):
    """Every test starts with an empty cache and a fake Shopify."""
    service.clear_cache()
    fake = FakeShopify()
    monkeypatch.setattr(service, "_shopify", lambda: fake)
    yield fake
    service.clear_cache()


# --- payload conversion --------------------------------------------------


async def test_only_active_products_reach_the_bot():
    """A draft or archived product must never be offered to a customer."""
    titles = [product.title for product in await service.get_catalog()]
    assert "Secret Sample" not in titles
    assert "Old Season" not in titles
    assert len(titles) == 4


async def test_variant_options_are_flattened_into_size_and_colour():
    product = await service.get_product("cairokee-t-shirt")
    assert [(v.size, v.color) for v in product.variants] == [
        ("S", "Brown"), ("S", "Black"), ("M", "Black"),
    ]
    assert product.sizes == ["S", "M"]
    assert product.colors == ["Brown", "Black"]


async def test_out_of_stock_variants_are_excluded_from_what_is_offered():
    product = await service.get_product("cairokee-hoodie")
    assert product.in_stock is True
    # M/Brown is unavailable, so Brown must not be offered.
    assert product.available_colors() == ["Black"]
    assert product.available_sizes() == ["S"]


async def test_online_url_stays_none_while_the_storefront_is_locked():
    """The real store has onlineStoreUrl null; the bot must not invent a link."""
    product = await service.get_product("cairokee-t-shirt")
    assert product.online_url is None
    assert "url" not in product.to_tool_dict()


async def test_url_is_passed_through_once_the_product_is_published(monkeypatch, fresh_catalog):
    fresh_catalog.nodes = [_node("Published Tee", online_url="https://shop.example/p/tee")]
    service.clear_cache()
    product = await service.get_product("published-tee")
    assert product.to_tool_dict()["url"] == "https://shop.example/p/tee"


# --- tool payload shape --------------------------------------------------


def test_price_label_collapses_an_identical_range():
    product = Product(id="1", title="Tee", handle="tee", price_min="600.0",
                      price_max="600.0", currency="EGP",
                      variants=[Variant(id="v", title="M", price="600.0", currency="EGP",
                                        available=True, options={"Size": "M"})])
    assert product.to_tool_dict()["price"] == "600 EGP"


def test_price_label_keeps_a_real_range():
    product = Product(id="1", title="Hoodie", handle="h", price_min="650.0",
                      price_max="700.0", currency="EGP")
    assert product.to_tool_dict()["price"] == "650-700 EGP"


def test_sold_out_product_is_labelled_rather_than_offered():
    product = Product(
        id="1", title="Tee", handle="tee", price_min="600.0", price_max="600.0",
        currency="EGP",
        variants=[Variant(id="v", title="M", price="600.0", currency="EGP",
                          available=False, options={"Size": "M"})],
    )
    payload = product.to_tool_dict()
    assert payload["in_stock"] is False
    assert payload["note"] == "every variant is currently out of stock"
    # No in-stock lists, so the model cannot offer a size it could not sell.
    assert "in_stock_sizes" not in payload
    assert "available" not in payload


# --- price and stock per colour ------------------------------------------
#
# Both faults below came from one live reply. The store prices by colour, and a colour
# is not made in every size, so two flat lists could not describe it truthfully.

def _ringer():
    """The real RINGER BOXY FIT TSHIRT: Burgundy is 500 and XL-only, the rest are 580."""
    variants = []
    for size in ("S", "M", "L", "XL"):
        for color in ("Brown", "Navy", "Beige"):
            variants.append(Variant(id=size + color, title=size + " / " + color,
                                    price="580.00", currency="EGP",
                                    available=size != "S",
                                    options={"Size": size, "Color": color}))
        variants.append(Variant(id=size + "Burgundy", title=size + " / Burgundy",
                                price="500.00", currency="EGP", available=size == "XL",
                                options={"Size": size, "Color": "Burgundy"}))
    return Product(id="1", title="RINGER BOXY FIT TSHIRT", handle="ringer",
                   price_min="500.0", price_max="580.0", currency="EGP",
                   variants=variants)


def test_each_colour_carries_its_own_price():
    """Live fault: a brown t-shirt quoted as "500 to 580 EGP" when brown is exactly 580."""
    rows = {row["color"]: row for row in _ringer().to_tool_dict()["available"]}

    assert rows["Brown"]["price"] == "580 EGP"
    assert rows["Burgundy"]["price"] == "500 EGP"


def test_sizes_belong_to_a_colour_not_to_the_product():
    """Live fault: "Brown, Navy, Beige, Burgundy in M, L, XL" - but Burgundy is XL only."""
    payload = _ringer().to_tool_dict()
    rows = {row["color"]: row for row in payload["available"]}

    assert rows["Burgundy"]["sizes"] == ["XL"]
    assert rows["Brown"]["sizes"] == ["M", "L", "XL"]
    # The flat list that made the wrong combinations readable must be gone.
    assert "in_stock_colors" not in payload
    assert "in_stock_sizes" not in payload


def test_a_sold_out_colour_is_not_listed_at_all():
    product = _ringer()
    for variant in product.variants:
        if variant.color == "Navy":
            variant.available = False

    colors = [row["color"] for row in product.to_tool_dict()["available"]]
    assert "Navy" not in colors


def test_price_for_a_colour_is_exact():
    product = _ringer()
    assert product.price_for_color("Burgundy") == "500 EGP"
    assert product.price_for_color("Brown") == "580 EGP"
    assert product.price_for_color(None) == ""
    assert product.price_for_color("Chartreuse") == ""


def test_a_product_without_colours_still_reports_its_sizes():
    """Not every product has a colour option; sizes are then all there is to say."""
    product = Product(
        id="1", title="Cap", handle="cap", price_min="200.0", price_max="200.0",
        currency="EGP",
        variants=[Variant(id="v1", title="One Size", price="200.0", currency="EGP",
                          available=True, options={"Size": "One Size"})],
    )
    payload = product.to_tool_dict()
    assert payload["in_stock_sizes"] == ["One Size"]
    assert "available" not in payload


def test_a_matched_photo_is_priced_by_the_colour_that_was_matched():
    from app.modules.catalog.schemas import ProductMatch

    payload = ProductMatch(product=_ringer(), color="Burgundy").to_tool_dict()

    assert payload["price"] == "500 EGP"
    assert payload["price_is_for_color"] == "Burgundy"


def test_an_unmatched_photo_keeps_the_product_range():
    from app.modules.catalog.schemas import ProductMatch

    payload = ProductMatch(product=_ringer()).to_tool_dict()

    assert payload["price"] == "500-580 EGP"
    assert "price_is_for_color" not in payload


# --- search --------------------------------------------------------------


async def test_english_keyword_search():
    found = await service.search_products("hoodie")
    assert [p.title for p in found] == ["Cairokee Hoodie", "WANAS HOODIE"]


async def test_arabic_query_is_mapped_onto_the_english_catalog():
    """Customers write Arabic; the catalog is in English."""
    found = await service.search_products("هودي")
    assert [p.title for p in found] == ["Cairokee Hoodie", "WANAS HOODIE"]


async def test_arabic_garment_plus_colour():
    found = await service.search_products("تيشيرت اسود")
    titles = [p.title for p in found]
    assert "Cairokee T-shirt" in titles


async def test_diacritics_and_alef_spellings_do_not_break_matching():
    assert await service.search_products("هُودي")
    assert await service.search_products("أسود")


async def test_partial_word_still_matches():
    """A customer typing "cairoke" should still find Cairokee."""
    assert [p.title for p in await service.search_products("cairoke")][0].startswith("Cairokee")


async def test_title_match_outranks_a_description_match():
    found = await service.search_products("oversized")
    # Only BOXY WNS TEE mentions oversized, and only in its description.
    assert [p.title for p in found] == ["BOXY WNS TEE"]


async def test_product_type_match_is_found_when_the_title_does_not_contain_the_word():
    found = await service.search_products("sweatshirts")
    assert {p.title for p in found} == {"Cairokee Hoodie", "WANAS HOODIE"}


async def test_unmatched_query_returns_nothing_rather_than_a_random_product():
    assert await service.search_products("leather handbag") == []
    assert await service.search_products("dress") == []


async def test_stopwords_alone_match_nothing():
    """"do you have any" must not match the whole catalog."""
    assert await service.search_products("do you have any") == []


async def test_blank_query_returns_nothing():
    assert await service.search_products("") == []
    assert await service.search_products("   ") == []


async def test_limit_is_respected():
    assert len(await service.search_products("t-shirt", limit=1)) == 1


# --- caching -------------------------------------------------------------


async def test_catalog_is_fetched_once_and_then_cached(fresh_catalog):
    await service.get_catalog()
    await service.get_catalog()
    await service.search_products("hoodie")
    assert fresh_catalog.fetches == 1


async def test_force_refresh_refetches(fresh_catalog):
    await service.get_catalog()
    await service.get_catalog(force_refresh=True)
    assert fresh_catalog.fetches == 2


async def test_stale_cache_is_served_when_shopify_goes_down(fresh_catalog):
    """A brief Shopify outage should not stop the bot answering."""
    await service.get_catalog()
    fresh_catalog.error = ShopifyUnavailable("503")
    products = await service.get_catalog(force_refresh=True)
    assert len(products) == 4


async def test_shopify_failure_with_no_cache_raises_rather_than_returning_empty(fresh_catalog):
    """An empty list would let the bot say "we have nothing", which is a lie."""
    fresh_catalog.error = ShopifyUnavailable("503")
    with pytest.raises(service.CatalogUnavailable):
        await service.get_catalog()


# --- browse (whole-catalog questions) ------------------------------------


async def test_browse_sorts_cheapest_first():
    """search_products cannot answer "cheapest"; browse sees the whole catalog."""
    found = await service.browse_products(limit=2)
    assert [p.title for p in found] == ["BOXY WNS TEE", "Cairokee T-shirt"]


async def test_browse_can_sort_most_expensive_first():
    found = await service.browse_products(sort="price_desc", limit=1)
    assert found[0].title == "Cairokee Hoodie"


async def test_browse_filters_by_max_price():
    found = await service.browse_products(max_price=600)
    assert {p.title for p in found} == {"BOXY WNS TEE", "Cairokee T-shirt"}


async def test_browse_filters_by_min_price():
    found = await service.browse_products(min_price=650)
    assert {p.title for p in found} == {"Cairokee Hoodie", "WANAS HOODIE"}


async def test_browse_filters_by_category_loosely():
    """"hoodie" singular must match the "Hoodies & Sweatshirts" product type."""
    found = await service.browse_products(category="hoodie")
    assert {p.title for p in found} == {"Cairokee Hoodie", "WANAS HOODIE"}


async def test_browse_accepts_an_arabic_category():
    found = await service.browse_products(category="هودي")
    assert {p.title for p in found} == {"Cairokee Hoodie", "WANAS HOODIE"}


async def test_browse_with_an_unknown_category_returns_nothing():
    assert await service.browse_products(category="handbags") == []


async def test_browse_respects_the_limit():
    assert len(await service.browse_products(limit=1)) == 1


async def test_browse_uses_the_cache_rather_than_refetching(fresh_catalog):
    await service.browse_products()
    await service.browse_products(category="hoodie")
    assert fresh_catalog.fetches == 1


async def test_categories_lists_real_product_types():
    assert await service.categories() == ["Hoodies & Sweatshirts", "T-Shirts"]


# --- is the shop actually reachable? -------------------------------------


def test_storefront_reads_as_closed_before_anything_is_cached():
    """Unknown must count as closed: sending a customer to a locked page is worse."""
    service.clear_cache()
    assert service.storefront_is_open() is False


async def test_storefront_is_closed_while_no_product_has_a_url():
    await service.get_catalog()
    assert service.storefront_is_open() is False


async def test_storefront_opens_by_itself_once_products_are_published(fresh_catalog):
    """Lifting the password needs no code change - Shopify starts sending the URL."""
    fresh_catalog.nodes = [_node("Published Tee", online_url="https://shop.example/p/tee")]
    service.clear_cache()
    await service.get_catalog()
    assert service.storefront_is_open() is True
