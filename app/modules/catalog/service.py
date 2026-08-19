"""Catalog module - the public surface for anything about products.

Owns two things the raw Shopify client deliberately does not: turning Shopify payloads
into our ``Product`` shape, and deciding which products actually answer a customer's
words. Nothing outside this module parses Shopify product JSON.

Search runs over a cached copy of the whole catalog rather than Shopify's own search.
With a catalog this size that is one cheap request, and it buys tolerant matching -
partial words, colours, sizes, product types and a small Arabic vocabulary - where
Shopify's query syntax would return nothing for "تيشيرت اسود" or "cairoke".
"""

import json
import logging
import re
import time
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.config import settings
from app.integrations import llm
from app.integrations.llm_types import ImagePart, LLMError, Turn
from app.integrations.shopify.client import ShopifyClient, ShopifyError
from app.modules.catalog.schemas import Identification, Product, ProductMatch, Variant

logger = logging.getLogger(__name__)

_client: Optional[ShopifyClient] = None
# (products, fetched_at) - the whole catalog, refetched when stale.
_cache: Optional[Tuple[List[Product], float]] = None


class CatalogUnavailable(Exception):
    """The catalog could not be read. Callers should say so, never invent products."""


def _shopify() -> ShopifyClient:
    global _client
    if _client is None:
        _client = ShopifyClient()
    return _client


# --- public API -----------------------------------------------------------


async def search_products(query: str, limit: int = 5) -> List[Product]:
    """Return the products that best match ``query``, most relevant first.

    An empty or unmatched query returns an empty list rather than a random selection -
    the bot must be able to say "I could not find that".
    """
    products = await get_catalog()
    terms = _terms(query)
    if not terms:
        return []

    scored = [
        (score, product)
        for score, product in ((_score(product, terms), product) for product in products)
        if score > 0
    ]
    scored.sort(key=lambda pair: (-pair[0], pair[1].title))
    matches = [product for _, product in scored[: max(1, limit)]]

    logger.info(
        "Catalog search %r -> %d/%d products: %s",
        query, len(matches), len(products), [p.title for p in matches],
    )
    return matches


async def browse_products(
    category: Optional[str] = None,
    max_price: Optional[float] = None,
    min_price: Optional[float] = None,
    sort: str = "price_asc",
    limit: int = 8,
) -> List[Product]:
    """List products by category and/or price, sorted - no keyword guessing involved.

    ``search_products`` cannot answer "what is your cheapest item?" or "anything under
    300", because a keyword search never sees the whole catalog. This does, using the
    cached copy, so it costs no extra Shopify request.

    ``category`` is matched against the product type loosely, so "hoodie", "hoodies" and
    "Hoodies & Sweatshirts" all work, as does Arabic through the same synonym map.
    """
    products = await get_catalog()

    if category:
        terms = _terms(category)
        products = [p for p in products if _matches_category(p, terms)]

    def price_of(product: Product) -> float:
        try:
            return float(product.price_min or 0)
        except ValueError:
            return 0.0

    if min_price is not None:
        products = [p for p in products if price_of(p) >= min_price]
    if max_price is not None:
        products = [p for p in products if price_of(p) <= max_price]

    if sort == "price_desc":
        products.sort(key=lambda p: (-price_of(p), p.title))
    else:
        products.sort(key=lambda p: (price_of(p), p.title))

    logger.info(
        "Catalog browse category=%r min=%s max=%s sort=%s -> %d products",
        category, min_price, max_price, sort, len(products),
    )
    return products[: max(1, limit)]


async def categories() -> List[str]:
    """Distinct product types in the catalog, so the bot can offer real categories."""
    seen: List[str] = []
    for product in await get_catalog():
        if product.product_type and product.product_type not in seen:
            seen.append(product.product_type)
    return sorted(seen)


async def get_catalog(force_refresh: bool = False) -> List[Product]:
    """The whole catalog, from cache when it is still fresh."""
    global _cache

    if not force_refresh and _cache is not None:
        products, fetched_at = _cache
        if time.monotonic() - fetched_at < settings.catalog_cache_seconds:
            return products

    try:
        nodes = await _shopify().fetch_all_products()
    except ShopifyError as exc:
        if _cache is not None:
            # A stale catalog beats no answer at all; the bot stays useful through a
            # brief Shopify outage.
            logger.warning("Shopify unreachable (%s); serving the cached catalog", exc)
            return _cache[0]
        logger.error("Could not read the catalog from Shopify: %s", exc)
        raise CatalogUnavailable(str(exc)) from exc

    products = [_to_product(node) for node in nodes if node]
    # Draft and archived products must never be offered to a customer.
    products = [p for p in products if p is not None]
    _cache = (products, time.monotonic())
    logger.info("Catalog loaded: %d products", len(products))
    return products


async def get_product(product_id: str) -> Optional[Product]:
    """Look up one product by its Shopify id or handle."""
    for product in await get_catalog():
        if product.id == product_id or product.handle == product_id:
            return product
    return None


class VariantNotFound(Exception):
    """No single buyable variant matches what was asked for.

    Carries ``available`` so the caller can tell the customer what there is instead of
    just failing - being told "that size is gone, but M and L are here" is the whole
    difference between a lost sale and a smaller one.
    """

    def __init__(self, message: str, product: Optional[Product] = None,
                 available: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.product = product
        self.available = available or []


async def resolve_variant(
    product_ref: str,
    size: Optional[str] = None,
    color: Optional[str] = None,
) -> Tuple[Product, Variant]:
    """Find the one in-stock variant matching a product and its size and colour.

    ``product_ref`` may be the Shopify id, the handle or the title - the model will have
    whichever the last tool result gave it. Raises ``VariantNotFound`` unless exactly one
    buyable variant matches, so an order is never built on a guess between two of them.
    """
    product = await _find_product(product_ref)
    if product is None:
        raise VariantNotFound("No product matches " + repr(product_ref))

    if not product.in_stock:
        raise VariantNotFound(product.title + " is entirely out of stock", product)

    wanted_size = _normalise(size or "")
    wanted_color = _normalise(color or "")

    candidates = [
        variant for variant in product.variants
        if variant.available
        and (not wanted_size or _normalise(variant.size or "") == wanted_size)
        and (not wanted_color or _normalise(variant.color or "") == wanted_color)
    ]

    if not candidates:
        # Distinguish "we never made that" from "it is gone", because only one of them
        # is worth apologising for.
        exists = any(
            (not wanted_size or _normalise(v.size or "") == wanted_size)
            and (not wanted_color or _normalise(v.color or "") == wanted_color)
            for v in product.variants
        )
        detail = "is sold out" if exists else "does not exist"
        raise VariantNotFound(
            product.title + " in " + (" / ".join(p for p in (color, size) if p) or "that option")
            + " " + detail,
            product, product.stock_by_color(),
        )

    if len(candidates) > 1:
        # Ambiguous - the customer named a colour but not a size, or the reverse.
        raise VariantNotFound(
            "More than one option of " + product.title + " matches; a size and colour are needed",
            product, product.stock_by_color(),
        )

    return product, candidates[0]


async def _find_product(reference: str) -> Optional[Product]:
    """Look a product up by Shopify id, handle or exact title."""
    reference = (reference or "").strip()
    if not reference:
        return None

    catalog = await get_catalog()
    wanted = _normalise(reference)
    for product in catalog:
        if reference == product.id or reference == product.handle:
            return product
    for product in catalog:
        if _normalise(product.title) == wanted or _normalise(product.handle) == wanted:
            return product
    return None


def storefront_is_open() -> bool:
    """Whether customers can actually reach the shop themselves.

    Reads the cached catalog only - never fetches - so it is safe to call while building
    a prompt. Shopify reports ``onlineStoreUrl`` for a product only once the storefront
    is published and unlocked, so one product with a URL means the doors are open.

    Unknown counts as closed: telling a customer to go and order on a site that turns out
    to be password-protected is worse than not mentioning it.
    """
    if _cache is None:
        return False
    products, _fetched_at = _cache
    return any(product.online_url for product in products)


def clear_cache() -> None:
    """Drop the cached catalog. Used by tests and after a known catalog change."""
    global _cache
    _cache = None


# --- Shopify payload -> our shapes ---------------------------------------


def _to_product(node: Dict[str, Any]) -> Optional[Product]:
    """Convert one Shopify product node, or None if it must not be sold."""
    if (node.get("status") or "").upper() != "ACTIVE":
        return None

    price_range = node.get("priceRangeV2") or {}
    minimum = price_range.get("minVariantPrice") or {}
    maximum = price_range.get("maxVariantPrice") or {}
    image = node.get("featuredImage") or {}

    return Product(
        id=node.get("id", ""),
        title=(node.get("title") or "").strip(),
        handle=node.get("handle") or "",
        product_type=(node.get("productType") or "").strip(),
        tags=list(node.get("tags") or []),
        description=(node.get("description") or "").strip(),
        price_min=str(minimum.get("amount") or ""),
        price_max=str(maximum.get("amount") or ""),
        currency=minimum.get("currencyCode") or "",
        image_url=image.get("url"),
        # Null while the storefront is password-protected or the product is unpublished.
        online_url=node.get("onlineStoreUrl"),
        total_inventory=node.get("totalInventory"),
        variants=[_to_variant(v, minimum.get("currencyCode") or "")
                  for v in ((node.get("variants") or {}).get("nodes") or [])],
    )


def _to_variant(node: Dict[str, Any], currency: str) -> Variant:
    options = {
        (option.get("name") or ""): (option.get("value") or "")
        for option in (node.get("selectedOptions") or [])
    }
    return Variant(
        id=node.get("id", ""),
        title=node.get("title") or "",
        price=str(node.get("price") or ""),
        currency=currency,
        available=bool(node.get("availableForSale")),
        inventory_quantity=node.get("inventoryQuantity"),
        sku=node.get("sku") or None,
        options=options,
    )


# --- matching -------------------------------------------------------------

# Arabic (and common transliteration) for garment types and colours, mapped onto the
# English words the catalog actually uses. The bot is told to search in English, but
# customers write Arabic and one missed translation means zero results.
_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "تيشيرت": ("t-shirt", "tee", "tshirt"),
    "تيشرت": ("t-shirt", "tee", "tshirt"),
    "تشيرت": ("t-shirt", "tee", "tshirt"),
    "قميص": ("shirt",),
    "هودي": ("hoodie",),
    "هودى": ("hoodie",),
    "سويت": ("sweatshirt", "sweat"),
    "سويتشيرت": ("sweatshirt",),
    "بنطلون": ("pants", "trouser", "jeans"),
    "بنطال": ("pants", "trouser"),
    "جاكيت": ("jacket",),
    "فستان": ("dress",),
    "عباية": ("abaya",),
    "عبايه": ("abaya",),
    "شورت": ("shorts",),
    "كاب": ("cap", "hat"),
    "حقيبة": ("bag",),
    "اسود": ("black",),
    "أسود": ("black",),
    "ابيض": ("white",),
    "أبيض": ("white",),
    "بيج": ("beige",),
    "بني": ("brown",),
    "بنى": ("brown",),
    "رمادي": ("grey", "gray"),
    "رمادى": ("grey", "gray"),
    "اخضر": ("green", "olive"),
    "أخضر": ("green", "olive"),
    "ازرق": ("blue",),
    "أزرق": ("blue",),
    "احمر": ("red",),
    "أحمر": ("red",),
    "زيتي": ("olive",),
}

# Words that carry no signal and would otherwise match everything.
_STOPWORDS = {
    "a", "an", "the", "and", "or", "for", "with", "in", "of", "me", "i", "you", "your",
    "do", "does", "have", "has", "any", "some", "show", "want", "need", "looking", "look",
    "please", "size", "color", "colour", "price", "cost", "much", "how", "what", "is",
    "are", "there", "available", "buy", "order", "product", "products", "item", "items",
    "عندكم", "عندك", "في", "من", "على", "هل", "ما", "كم", "سعر", "مقاس", "لون", "عايز",
    "عاوز", "اريد", "أريد", "ابحث", "لدي", "لديكم", "هذا", "هذه",
}

_ARABIC_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")


def _normalise(text: str) -> str:
    """Lowercase, strip Arabic diacritics and unify alef/ya/ta-marbuta spellings."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = _ARABIC_DIACRITICS.sub("", text)
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ة", "ه")
    return text


def _terms(query: str) -> List[str]:
    """Split a customer's words into search terms, expanded through the synonym map."""
    normalised = _normalise(query)
    raw = [word for word in re.split(r"[^\w؀-ۿ-]+", normalised) if word]

    terms: List[str] = []
    for word in raw:
        for expansion in _SYNONYMS.get(word, ()):  # Arabic -> catalog English
            if expansion not in terms:
                terms.append(expansion)
        if word in _STOPWORDS or len(word) < 2:
            continue
        if word not in terms:
            terms.append(word)
        # "t-shirt" should also match a title written "tshirt" or "t shirt".
        if "-" in word:
            collapsed = word.replace("-", "")
            if collapsed and collapsed not in terms:
                terms.append(collapsed)
    return terms


def _matches_category(product: Product, terms: Sequence[str]) -> bool:
    """Loose product-type match, so "hoodie" finds "Hoodies & Sweatshirts"."""
    product_type = _normalise(product.product_type)
    tags = _normalise(" ".join(product.tags))
    for term in terms:
        stem = term[:-1] if term.endswith("s") and len(term) > 3 else term
        if stem and (stem in product_type or stem in tags):
            return True
    return False


def _score(product: Product, terms: Sequence[str]) -> int:
    """Weight a match by where it was found: title beats type beats description."""
    title = _normalise(product.title)
    title_collapsed = title.replace("-", "").replace(" ", "")
    product_type = _normalise(product.product_type)
    tags = _normalise(" ".join(product.tags))
    description = _normalise(product.description)
    options = _normalise(" ".join(
        value for variant in product.variants for value in variant.options.values()
    ))

    score = 0
    for term in terms:
        collapsed = term.replace("-", "")
        if term in title or (len(collapsed) > 2 and collapsed in title_collapsed):
            score += 10
        elif term in product_type:
            score += 6
        elif term in tags:
            score += 4
        elif term in options:
            # Matches a size or colour the product actually offers, e.g. "black".
            score += 3
        elif term in description:
            score += 1

    # Prefer something the customer can actually buy, without hiding the rest.
    if score > 0 and product.in_stock:
        score += 2
    return score


# --- identifying a product from a photo -----------------------------------
#
# Section 6's approach: show the photo to a model together with a compact list of the
# real catalog, and let it name candidates from that list. No embeddings or vector store
# - at eighteen products they would be engineering for a problem this store does not
# have.
#
# The store owner chose "assert when confident, ask otherwise". That makes the confidence
# value the load-bearing part, so it is not taken from the model. The model's own claim
# is only the first of four conditions in `_verify()`; a claim that survives all four is
# the only thing the bot is allowed to state as fact.

_IDENTIFY_SYSTEM = """You match photographs of clothing against a shop's product list.

You are given one or more photos and the shop's complete catalog. Name the catalog \
products that could be the garment in the photo.

Rules:
- Only ever name products from the catalog list you are given, spelled exactly as they \
appear there. Never invent a product.
- Judge on what is visible: garment type, colour, cut, collar and cuff detail, print or \
graphic, fastenings, texture.
- Say "high" confidence only when the visible details positively agree with one product \
and you would be surprised to be wrong. If the photo could be any plain garment of that \
type, that is "low" - a plain black t-shirt is not identifiable.
- If nothing in the catalog could be it, return an empty candidate list. That is a \
useful answer, not a failure.
- The photo may be of another brand entirely. Looking similar is not being the same \
product.

Reply with JSON only, in this shape:
{"description": "what is visible in the photo, one short phrase",
 "candidates": [{"title": "exact catalog title", "color": "colour seen, or null",
                 "confidence": "high|medium|low", "evidence": "the detail that matched"}]}
Order candidates best first, at most three."""


async def identify_product_from_image(
    images: Sequence[ImagePart],
    max_candidates: int = 3,
) -> Identification:
    """Identify which catalog products a customer's photo might show.

    Raises ``CatalogUnavailable`` if the catalog cannot be read; a failure of the
    identifying model itself comes back as an empty, unconfident result rather than an
    exception, because "I could not tell" is a perfectly good answer to give a customer.
    """
    if not images:
        return Identification(reason="no image was supplied")

    catalog = await get_catalog()
    if not catalog:
        raise CatalogUnavailable("The catalog is empty")

    prompt = (
        "Here is the shop's complete catalog:\n\n"
        + _catalog_index(catalog)
        + "\n\nWhich of these, if any, is the garment in the photo?"
    )

    try:
        response = await llm.generate(
            turns=[Turn(role="user", text=prompt, images=list(images))],
            system_instruction=_IDENTIFY_SYSTEM,
            json_output=True,
            # Identification is a matching task, not a creative one.
            temperature=0.0,
            # Optionally a different model from the one holding the conversation:
            # recognising a garment and talking to a customer are not the same skill.
            model=settings.gemini_vision_model or None,
        )
    except LLMError as exc:
        logger.warning("Image identification failed: %s", exc)
        return Identification(reason="the identifying model was unavailable")

    parsed = _parse_identification(response.text)
    if parsed is None:
        logger.warning("Image identification returned unparseable output: %r",
                       response.text[:300])
        return Identification(reason="the identifying model returned nothing usable")

    return _verify(parsed, catalog, max_candidates)


def _catalog_index(products: Sequence[Product]) -> str:
    """The whole catalog as compact lines - titles, type and colours, no prose.

    Product images are deliberately not sent: eighteen photos would dominate the request
    for no gain, since the titles and colours are what the model has to choose between.
    """
    lines = []
    for product in products:
        parts = [product.title]
        if product.product_type:
            parts.append("(" + product.product_type + ")")
        if product.colors:
            parts.append("colours: " + ", ".join(product.colors))
        lines.append("- " + " ".join(parts))
    return "\n".join(lines)


def _parse_identification(text: str) -> Optional[Dict[str, Any]]:
    """Read the identifier's JSON, tolerating a stray code fence or preamble."""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?|```$", "", candidate, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(candidate)
    except ValueError:
        # Some models still wrap JSON in a sentence; take the outermost object.
        found = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not found:
            return None
        try:
            parsed = json.loads(found.group(0))
        except ValueError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _verify(parsed: Dict[str, Any], catalog: Sequence[Product],
            max_candidates: int) -> Identification:
    """Turn the model's claim into a checked result.

    Four conditions have to hold before the bot may state a match as fact. Each one has
    a plausible failure behind it: a hallucinated title, a colour the product is not made
    in, a description so generic that several products fit it equally, or a model that
    was not sure in the first place.
    """
    description = str(parsed.get("description") or "").strip()
    by_title = {_normalise(product.title): product for product in catalog}

    matches: List[ProductMatch] = []
    dropped: List[str] = []
    for raw in (parsed.get("candidates") or [])[: max_candidates * 2]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        product = by_title.get(_normalise(title))
        if product is None:
            # The model named something that is not in the catalog it was given.
            dropped.append(title)
            continue

        claimed_color = raw.get("color") or ""
        color = _resolve_color(product, str(claimed_color))
        matches.append(ProductMatch(
            product=product,
            color=color,
            claimed_confidence=str(raw.get("confidence") or "").strip().lower(),
            evidence=str(raw.get("evidence") or "").strip(),
        ))
        if len(matches) == max_candidates:
            break

    if dropped:
        logger.warning("Identifier named %d product(s) that are not in the catalog: %s",
                       len(dropped), ", ".join(dropped[:3]))

    if not matches:
        return Identification(description=description,
                              reason="nothing in the catalog matched")

    top = matches[0]
    runner_up = matches[1] if len(matches) > 1 else None

    if top.claimed_confidence != "high":
        reason = "the identifier was not sure"
    elif runner_up is not None and runner_up.claimed_confidence == "high":
        # Two products fit the photo equally well, so neither can be asserted.
        reason = "more than one product fits the photo equally"
    elif _claimed_a_colour(parsed) and top.color is None:
        # It saw a colour this product is not made in - so it is not this product.
        reason = "the colour in the photo is not one this product comes in"
    else:
        return Identification(description=description, matches=matches, confident=True)

    logger.info("Withheld confidence on an image match: %s", reason)
    return Identification(description=description, matches=matches, reason=reason)


def _claimed_a_colour(parsed: Dict[str, Any]) -> bool:
    candidates = parsed.get("candidates") or []
    return bool(candidates and isinstance(candidates[0], dict)
                and str(candidates[0].get("color") or "").strip())


def _resolve_color(product: Product, claimed: str) -> Optional[str]:
    """The product's own spelling of a claimed colour, or None if it has no such option."""
    claimed = _normalise(claimed)
    if not claimed:
        return None
    for color in product.colors:
        normalised = _normalise(color)
        # "light blue" against "Light Blue", and "blue" against "Navy Blue".
        if normalised == claimed or claimed in normalised or normalised in claimed:
            return color
    return None
