"""What the store charges to deliver, read from the store itself.

A COD order has no checkout to work the delivery charge out, so the bot has to put a
shipping line on the order by hand - and the amount had better be the one the shop
actually charges, because it is the cash the courier collects at the door.

Shopify is the source of truth here rather than a number in configuration: the owner
changes rates in the admin, and nobody should have to remember to change a setting too.
``COD_SHIPPING_FEE`` exists only as a fallback for when the rate cannot be read at all.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.config import settings
from app.integrations.shopify.client import ShopifyClient, ShopifyError

logger = logging.getLogger(__name__)

# (rates, fetched_at). Rates change rarely, so this is cached far longer than the catalog.
_cache: Optional[Tuple[List["Rate"], float]] = None


@dataclass
class Rate:
    """One delivery option the store offers."""

    title: str
    amount: str
    currency: str
    # Empty means the zone covers the whole country rather than named provinces.
    provinces: Tuple[str, ...] = ()
    # Minimum order value this rate applies from, when the store set one.
    min_subtotal: Optional[float] = None
    max_subtotal: Optional[float] = None

    def applies_to(self, province: Optional[str], subtotal: float) -> bool:
        if self.provinces and (province or "") not in self.provinces:
            return False
        return self.applies_to_value(subtotal)

    def applies_to_value(self, subtotal: float) -> bool:
        """The order-value conditions only, ignoring where it is going."""
        if self.min_subtotal is not None and subtotal < self.min_subtotal:
            return False
        if self.max_subtotal is not None and subtotal > self.max_subtotal:
            return False
        return True

    @property
    def value(self) -> float:
        try:
            return float(self.amount)
        except (TypeError, ValueError):
            return 0.0


def clear_cache() -> None:
    global _cache
    _cache = None


async def rate_for(
    client: ShopifyClient,
    province: Optional[str],
    subtotal: float,
) -> Optional[Rate]:
    """The delivery rate to charge for this destination, or None if none can be read.

    None is not an error: without a rate the order simply carries no delivery line, which
    is better than inventing a charge the customer never agreed to.
    """
    rates = await _rates(client)
    eligible = [rate for rate in rates if rate.applies_to(province, subtotal)]

    if not eligible and province is None:
        # The destination has not been pinned down yet - usually a customer asking the
        # price before giving an address. That is answerable whenever every rate charges
        # the same anyway, which is how this store is set up.
        anywhere = [rate for rate in rates if rate.applies_to_value(subtotal)]
        if anywhere and len({rate.amount for rate in anywhere}) == 1:
            return anywhere[0]
        if anywhere:
            logger.info("Delivery differs by destination; cannot quote without a governorate")
            return None

    if not eligible:
        if rates:
            logger.warning("No delivery rate covers province %r at %.2f", province, subtotal)
        return None

    # Cheapest wins when a store offers several. Picking arbitrarily between a standard
    # and an express rate would silently overcharge; the customer never chose express.
    eligible.sort(key=lambda rate: rate.value)
    if len(eligible) > 1:
        logger.info("%d delivery rates apply; charging the cheapest (%s)",
                    len(eligible), eligible[0].title)
    return eligible[0]


async def _rates(client: ShopifyClient) -> List[Rate]:
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[1] < settings.shipping_cache_seconds:
        return _cache[0]

    try:
        profiles = await client.fetch_delivery_rates()
    except ShopifyError as exc:
        # Missing scope, or Shopify is down. Serve a stale copy if there is one, since a
        # rate from ten minutes ago is far better than no delivery line at all.
        logger.warning("Could not read delivery rates: %s", exc)
        return _cache[0] if _cache is not None else []

    rates = _parse(profiles)
    if not rates:
        logger.warning("The store has no fixed delivery rates configured")
    _cache = (rates, now)
    return rates


def _parse(profiles: Sequence[Dict[str, Any]]) -> List[Rate]:
    """Flatten Shopify's profile/zone/method nesting into the rates we can actually use."""
    country = (settings.store_country_code or "").upper()
    rates: List[Rate] = []

    for profile in profiles:
        for group in profile.get("profileLocationGroups") or []:
            for node in (group.get("locationGroupZones") or {}).get("nodes") or []:
                zone = node.get("zone") or {}
                provinces = _zone_provinces(zone, country)
                if provinces is None:
                    continue  # a zone for somewhere else entirely

                for method in (node.get("methodDefinitions") or {}).get("nodes") or []:
                    rate = _to_rate(method, provinces)
                    if rate is not None:
                        rates.append(rate)
    return rates


def _zone_provinces(zone: Dict[str, Any], country: str) -> Optional[Tuple[str, ...]]:
    """The provinces this zone covers, () for the whole country, None if it is elsewhere."""
    for entry in zone.get("countries") or []:
        code = (entry.get("code") or {})
        if code.get("countryCode") == country:
            return tuple(p.get("code") for p in (entry.get("provinces") or []) if p.get("code"))
        if code.get("restOfWorld"):
            # A catch-all zone; only used when no zone names this country outright.
            return ()
    return None


def _to_rate(method: Dict[str, Any], provinces: Tuple[str, ...]) -> Optional[Rate]:
    if not method.get("active"):
        return None

    provider = method.get("rateProvider") or {}
    price = provider.get("price")
    if not price:
        # A carrier-calculated rate. It needs a live quote from the carrier, which a
        # chat order cannot get, so it is skipped rather than guessed at.
        logger.info("Skipping carrier-calculated rate %r", method.get("name"))
        return None

    minimum = maximum = None
    for condition in method.get("methodConditions") or []:
        criteria = condition.get("conditionCriteria") or {}
        if criteria.get("__typename") != "MoneyV2":
            # Weight-based conditions need item weights, which this store does not set.
            logger.info("Ignoring non-price condition on rate %r", method.get("name"))
            continue
        try:
            amount = float(criteria.get("amount"))
        except (TypeError, ValueError):
            continue
        if str(condition.get("operator", "")).upper().startswith("GREATER"):
            minimum = amount
        else:
            maximum = amount

    return Rate(
        title=str(method.get("name") or "Delivery"),
        amount=str(price.get("amount") or "0"),
        currency=str(price.get("currencyCode") or settings.store_currency),
        provinces=provinces,
        min_subtotal=minimum,
        max_subtotal=maximum,
    )
