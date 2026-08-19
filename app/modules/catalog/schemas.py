"""Data shapes the catalog module exposes.

These are the catalog's public vocabulary. Other modules and the chat tools work with
these, never with Shopify's raw GraphQL payloads - so a change in Shopify's API surface
stops at the catalog boundary.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Variant:
    """One buyable combination of options, e.g. size M in Black."""

    id: str
    title: str
    price: str
    currency: str
    available: bool
    inventory_quantity: Optional[int] = None
    sku: Optional[str] = None
    # Flattened from Shopify's selectedOptions, e.g. {"Size": "M", "Color": "Black"}.
    options: Dict[str, str] = field(default_factory=dict)

    @property
    def size(self) -> Optional[str]:
        return self.options.get("Size")

    @property
    def color(self) -> Optional[str]:
        return self.options.get("Color")


@dataclass
class Product:
    """A catalog product with everything the bot needs to describe it honestly."""

    id: str
    title: str
    handle: str
    product_type: str = ""
    tags: List[str] = field(default_factory=list)
    description: str = ""
    price_min: str = ""
    price_max: str = ""
    currency: str = ""
    image_url: Optional[str] = None
    # Only set when the product is genuinely reachable by a customer. Stays None while
    # the storefront is password-protected or the product is unpublished, so the bot
    # never offers a link that leads nowhere.
    online_url: Optional[str] = None
    total_inventory: Optional[int] = None
    variants: List[Variant] = field(default_factory=list)

    @property
    def in_stock(self) -> bool:
        return any(variant.available for variant in self.variants)

    @property
    def sizes(self) -> List[str]:
        """Distinct sizes, in the order Shopify returns them."""
        return _distinct(variant.size for variant in self.variants)

    @property
    def colors(self) -> List[str]:
        return _distinct(variant.color for variant in self.variants)

    def available_sizes(self) -> List[str]:
        return _distinct(v.size for v in self.variants if v.available)

    def available_colors(self) -> List[str]:
        return _distinct(v.color for v in self.variants if v.available)

    def price_for_color(self, color: Optional[str]) -> str:
        """What one colour actually costs, in stock.

        This store prices by colour - the same t-shirt is 500 EGP in Burgundy and 580 in
        Brown - so the product-level range is never the answer to "how much is the brown
        one?".
        """
        if not color:
            return ""
        variants = [v for v in self.variants
                    if v.color == color and v.available] or [
                        v for v in self.variants if v.color == color]
        return _price_range(variants, self.currency)

    def stock_by_color(self) -> List[Dict[str, Any]]:
        """In-stock options grouped by colour: what it costs and which sizes exist.

        Sizes belong to a colour, not to the product. Listing them separately says
        "Burgundy, in M, L or XL" when Burgundy was only ever made in XL.
        """
        groups = []
        for color in self.available_colors():
            variants = [v for v in self.variants if v.color == color and v.available]
            group: Dict[str, Any] = {"color": color}
            price = _price_range(variants, self.currency)
            if price:
                group["price"] = price
            sizes = _distinct(v.size for v in variants)
            if sizes:
                group["sizes"] = sizes
            groups.append(group)
        return groups

    def to_tool_dict(self) -> Dict[str, Any]:
        """Compact JSON for the model.

        Deliberately not the full variant matrix: a product with 12 variants would
        otherwise dominate the context. Sizes, colours and what is actually in stock are
        what the bot needs to answer and to guide an order.
        """
        payload: Dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "product_type": self.product_type,
            "price": _price_label(self.price_min, self.price_max, self.currency),
            "in_stock": self.in_stock,
            "sizes": self.sizes,
            "colors": self.colors,
        }
        if not self.in_stock:
            payload["note"] = "every variant is currently out of stock"
        elif self.colors:
            # Per colour, because both price and available sizes depend on it. Flat
            # lists of colours and sizes imply combinations that do not exist.
            payload["available"] = self.stock_by_color()
        else:
            # No colour option on this product; sizes are all there is to report.
            payload["in_stock_sizes"] = self.available_sizes()
        if self.description:
            payload["description"] = self.description
        if self.tags:
            payload["tags"] = self.tags
        if self.online_url:
            payload["url"] = self.online_url
        return payload


def _distinct(values) -> List[str]:
    seen: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def _price_range(variants, currency: str) -> str:
    """One price label covering a set of variants."""
    prices = [v.price for v in variants if v.price]
    if not prices:
        return ""
    try:
        low, high = min(prices, key=float), max(prices, key=float)
    except ValueError:
        low = high = prices[0]
    return _price_label(low, high, currency)


def _price_label(minimum: str, maximum: str, currency: str) -> str:
    """One human-readable price, collapsing an identical range to a single figure."""
    low = _trim(minimum)
    high = _trim(maximum)
    if not low:
        return ""
    if high and high != low:
        return low + "-" + high + " " + currency
    return low + " " + currency


def _trim(amount: str) -> str:
    """Shopify returns "600.0"; show "600" and keep real decimals."""
    if not amount:
        return ""
    text = str(amount)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass
class ProductMatch:
    """One catalog product put forward as what a customer's photo might show."""

    product: Product
    # The colour the identifier claimed to see, already checked against the product's
    # real options. None when it named no colour, or named one this product does not have.
    color: Optional[str] = None
    # What the identifying model reported, before our own checks. Kept for the log:
    # it is the claim, not the verdict.
    claimed_confidence: str = ""
    # The visible detail it matched on, e.g. "brown body with light blue collar trim".
    evidence: str = ""

    def to_tool_dict(self) -> Dict[str, Any]:
        payload = self.product.to_tool_dict()
        if self.color:
            payload["matched_color"] = self.color
            # The customer is asking about the piece in their photo, so "price" should
            # be what that colour costs - not a range spanning colours they did not ask
            # about. Live fault: a brown t-shirt quoted as "500 to 580 EGP" when brown
            # is exactly 580.
            exact = self.product.price_for_color(self.color)
            if exact:
                payload["price"] = exact
                payload["price_is_for_color"] = self.color
        if self.evidence:
            payload["visible_detail"] = self.evidence
        return payload


@dataclass
class Identification:
    """The result of looking at a photo against the catalog.

    ``confident`` is *earned*, not reported: it is set only when the identifying model
    said it was sure, the product it named actually exists, any colour it claimed is a
    real option on that product, and nothing else scored as highly. A model's own
    confidence is not calibrated enough to act on by itself.
    """

    description: str = ""
    matches: List["ProductMatch"] = field(default_factory=list)
    confident: bool = False
    # Why confidence was withheld, in factual terms. For the log, and to explain the
    # decision when reading it back later.
    reason: str = ""

    def to_tool_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "seen_in_photo": self.description,
            "confident": self.confident,
            "count": len(self.matches),
            "matches": [match.to_tool_dict() for match in self.matches],
        }
        return payload
