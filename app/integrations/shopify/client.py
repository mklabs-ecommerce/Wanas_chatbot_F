"""Raw Shopify Admin API client.

A dumb wrapper. It knows how to authenticate, how to send a GraphQL document, how to
survive Shopify's throttling, and how to hand back the raw payload. It knows nothing
about what a "COD order" is, how to rank a search result, or what the bot should say -
that lives in the module services.

GraphQL rather than REST: the REST products endpoint has no keyword search, and REST is
the legacy surface. One GraphQL call returns a product with its variants, options,
prices, stock and image together.
"""

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class ShopifyError(Exception):
    """Any Shopify failure."""


class ShopifyNotConfigured(ShopifyError):
    """Store domain or access token is missing."""


class ShopifyAuthError(ShopifyError):
    """Token rejected or lacking a required scope - retrying will not help."""


class ShopifyThrottled(ShopifyError):
    """Rate or cost limit hit; worth retrying after a pause."""


class ShopifyUnavailable(ShopifyError):
    """Transport or 5xx failure; worth retrying."""


class ShopifyRejected(ShopifyError):
    """The API understood the request and refused it (a mutation's userErrors).

    Separate from ShopifyError because these are the caller's problem to explain - an
    out-of-stock variant or a malformed address - not an outage to apologise for.
    """

    def __init__(self, message: str, errors: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.errors = errors or []


# One document reused for both listing and searching: passing `query` filters, omitting
# it returns everything. `truncateAt` keeps descriptions from bloating the payload.
PRODUCTS_QUERY = """
query Products($first: Int!, $after: String, $query: String) {
  products(first: $first, after: $after, query: $query) {
    nodes {
      id
      title
      handle
      productType
      tags
      status
      onlineStoreUrl
      totalInventory
      description(truncateAt: 400)
      featuredImage { url altText }
      priceRangeV2 {
        minVariantPrice { amount currencyCode }
        maxVariantPrice { amount currencyCode }
      }
      variants(first: 100) {
        nodes {
          id
          title
          sku
          price
          availableForSale
          inventoryQuantity
          selectedOptions { name value }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Orders. `query` is Shopify's own search syntax; note that it matches loosely (a
# `phone:` filter returns near-misses too), so callers must confirm identity themselves -
# see `modules/orders/service.py`. Everything a customer could reasonably ask about an
# order is fetched in one call; deciding what may be shown is the module's job.
ORDERS_QUERY = """
query Orders($first: Int!, $query: String) {
  orders(first: $first, query: $query, sortKey: CREATED_AT, reverse: true) {
    nodes {
      id
      name
      createdAt
      cancelledAt
      cancelReason
      displayFinancialStatus
      displayFulfillmentStatus
      email
      phone
      tags
      totalPriceSet { shopMoney { amount currencyCode } }
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      subtotalPriceSet { shopMoney { amount currencyCode } }
      totalShippingPriceSet { shopMoney { amount currencyCode } }
      shippingLine { title }
      customer { email phone }
      shippingAddress { name phone city province country }
      lineItems(first: 25) {
        nodes { title quantity variantTitle sku }
      }
      fulfillments(first: 5) {
        status
        createdAt
        estimatedDeliveryAt
        trackingInfo { number url company }
      }
    }
  }
}
"""

# Creating an order outright, as cash on delivery needs - there is no checkout step for
# the customer to go through. What the fields should contain is `modules/orders`' business;
# this document only knows how to carry them. The returned selection matches ORDERS_QUERY
# so one mapper can read either.
ORDER_CREATE_MUTATION = """
mutation CreateOrder($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {
  orderCreate(order: $order, options: $options) {
    order {
      id
      name
      createdAt
      cancelledAt
      cancelReason
      displayFinancialStatus
      displayFulfillmentStatus
      email
      phone
      tags
      totalPriceSet { shopMoney { amount currencyCode } }
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      subtotalPriceSet { shopMoney { amount currencyCode } }
      totalShippingPriceSet { shopMoney { amount currencyCode } }
      shippingLine { title }
      customer { email phone }
      shippingAddress { name phone city province country }
      lineItems(first: 25) { nodes { title quantity variantTitle sku } }
      fulfillments(first: 5) {
        status
        createdAt
        estimatedDeliveryAt
        trackingInfo { number url company }
      }
    }
    userErrors { field message }
  }
}
"""

# Cancelling an order. Shopify runs this as a background job, so the mutation returning
# cleanly means "accepted", not "already done" - the caller re-reads the order to see the
# result. `refund` and `restock` are required by the API and are decided by the caller,
# not defaulted here: this client knows the wire format and nothing about the business.
ORDER_CANCEL_MUTATION = """
mutation CancelOrder($orderId: ID!, $reason: OrderCancelReason!, $refund: Boolean!,
                     $restock: Boolean!, $notifyCustomer: Boolean, $staffNote: String) {
  orderCancel(orderId: $orderId, reason: $reason, refund: $refund, restock: $restock,
              notifyCustomer: $notifyCustomer, staffNote: $staffNote) {
    job { id done }
    orderCancelUserErrors { field message code }
  }
}
"""

# What the store charges for delivery. Needs the read_shipping scope. Carrier-calculated
# rates come back as DeliveryParticipant with no price - deciding what to do about that
# is the orders module's problem, not this one's.
DELIVERY_RATES_QUERY = """
{ deliveryProfiles(first: 5) { nodes {
    name
    default
    profileLocationGroups { locationGroupZones(first: 25) { nodes {
      zone {
        name
        countries { code { countryCode restOfWorld } provinces { code } }
      }
      methodDefinitions(first: 20) { nodes {
        name
        active
        rateProvider {
          __typename
          ... on DeliveryRateDefinition { price { amount currencyCode } }
        }
        methodConditions {
          field
          operator
          conditionCriteria {
            __typename
            ... on MoneyV2 { amount currencyCode }
            ... on Weight { value unit }
          }
        }
      } }
    } } }
} } }
"""

# Customers, so a cash-on-delivery order is not filed under "No customer". Shopify's
# search matches loosely here as it does everywhere else, so the caller re-checks.
CUSTOMERS_QUERY = """
query FindCustomers($query: String!) {
  customers(first: 5, query: $query) {
    nodes {
      id
      defaultPhoneNumber { phoneNumber }
      defaultEmailAddress { emailAddress }
    }
  }
}
"""

CUSTOMER_CREATE_MUTATION = """
mutation CreateCustomer($input: CustomerInput!) {
  customerCreate(input: $input) {
    customer { id }
    userErrors { field message }
  }
}
"""

# Minimal document used only for the /health probe.
SHOP_PING_QUERY = "{ shop { name myshopifyDomain currencyCode } }"


class ShopifyClient:
    """Thin async wrapper over the Shopify Admin GraphQL API."""

    def __init__(self, store: Optional[str] = None, token: Optional[str] = None) -> None:
        self._store = store if store is not None else settings.shopify_store
        self._token = token if token is not None else settings.shopify_access_token
        if not self._store or not self._token:
            raise ShopifyNotConfigured(
                "SHOPIFY_STORE and SHOPIFY_ACCESS_TOKEN must both be set"
            )
        self._url = (
            "https://" + self._store + "/admin/api/" + settings.shopify_api_version
            + "/graphql.json"
        )
        self._headers = {
            "X-Shopify-Access-Token": self._token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # -- public API ------------------------------------------------------
    async def graphql(
        self,
        document: str,
        variables: Optional[Dict[str, Any]] = None,
        max_attempts: int = 3,
    ) -> Dict[str, Any]:
        """Run a GraphQL document and return its ``data``.

        Retries throttling and transport failures; auth and query errors are raised
        immediately because repeating them cannot help.
        """
        payload: Dict[str, Any] = {"query": document}
        if variables:
            payload["variables"] = variables

        last_error: ShopifyError = ShopifyUnavailable("no attempt was made")
        timeout = httpx.Timeout(settings.shopify_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as http:
            for attempt in range(1, max_attempts + 1):
                try:
                    response = await http.post(self._url, headers=self._headers, json=payload)
                    return _parse(response)
                except (ShopifyThrottled, ShopifyUnavailable) as exc:
                    last_error = exc
                except httpx.HTTPError as exc:
                    last_error = ShopifyUnavailable(type(exc).__name__ + ": " + str(exc))

                if attempt == max_attempts:
                    break
                delay = min(6.0, 0.7 * (2 ** (attempt - 1))) + random.uniform(0, 0.3)
                logger.warning(
                    "Shopify attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt, max_attempts, last_error, delay,
                )
                await asyncio.sleep(delay)

        raise last_error

    async def fetch_products(
        self,
        query: Optional[str] = None,
        first: int = 50,
        after: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return one page of raw product nodes plus the next cursor.

        ``query`` is Shopify's own search syntax, passed straight through. Relevance
        ranking and any catalog-level logic belong to ``modules/catalog``.
        """
        variables: Dict[str, Any] = {"first": max(1, min(first, 250)), "after": after}
        if query:
            variables["query"] = query

        data = await self.graphql(PRODUCTS_QUERY, variables)
        products = data.get("products") or {}
        page = products.get("pageInfo") or {}
        cursor = page.get("endCursor") if page.get("hasNextPage") else None
        return products.get("nodes") or [], cursor

    async def fetch_all_products(self, page_size: int = 100, max_pages: int = 20) -> List[Dict[str, Any]]:
        """Walk every page of the catalog.

        ``max_pages`` is a guard so a pagination bug can never loop forever.
        """
        nodes: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            page, cursor = await self.fetch_products(first=page_size, after=cursor)
            nodes.extend(page)
            if not cursor:
                break
        else:
            logger.warning("Stopped paginating products after %d pages", max_pages)
        return nodes

    async def fetch_orders(
        self,
        query: Optional[str] = None,
        first: int = 10,
    ) -> List[Dict[str, Any]]:
        """Return raw order nodes matching Shopify's search ``query``, newest first.

        Deliberately unpaginated: every caller wants a handful of recent orders, and an
        order lookup that walks the whole history would be both slow and pointless.
        """
        variables: Dict[str, Any] = {"first": max(1, min(first, 50))}
        if query:
            variables["query"] = query

        data = await self.graphql(ORDERS_QUERY, variables)
        return (data.get("orders") or {}).get("nodes") or []

    async def create_order(
        self,
        order: Dict[str, Any],
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create an order and return the raw order node.

        Not retried. Every other call here is safe to repeat; this one creates something
        real, and a retry after an ambiguous failure would risk a second order for the
        same customer.
        """
        data = await self.graphql(
            ORDER_CREATE_MUTATION,
            {"order": order, "options": options or {}},
            max_attempts=1,
        )
        payload = data.get("orderCreate") or {}
        errors = payload.get("userErrors") or []
        if errors:
            message = "; ".join(
                (".".join(item.get("field") or []) + ": " if item.get("field") else "")
                + str(item.get("message", item))
                for item in errors
            )
            raise ShopifyRejected("Shopify refused the order: " + message[:400], errors)

        created = payload.get("order")
        if not created:
            raise ShopifyError("Shopify returned no order and no error")
        return created

    async def cancel_order(
        self,
        order_id: str,
        reason: str = "CUSTOMER",
        refund: bool = False,
        restock: bool = True,
        notify_customer: bool = False,
        staff_note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Ask Shopify to cancel an order. Returns the job node.

        Not retried, for the same reason ``create_order`` is not: this changes something
        real, and repeating an ambiguous failure is worse than reporting it.
        """
        data = await self.graphql(
            ORDER_CANCEL_MUTATION,
            {
                "orderId": order_id,
                "reason": reason,
                "refund": refund,
                "restock": restock,
                "notifyCustomer": notify_customer,
                "staffNote": staff_note,
            },
            max_attempts=1,
        )
        payload = data.get("orderCancel") or {}
        errors = payload.get("orderCancelUserErrors") or []
        if errors:
            message = "; ".join(
                (".".join(item.get("field") or []) + ": " if item.get("field") else "")
                + str(item.get("message", item))
                for item in errors
            )
            raise ShopifyRejected("Shopify refused to cancel the order: " + message[:400],
                                  errors)
        return payload.get("job") or {}

    async def fetch_delivery_rates(self) -> List[Dict[str, Any]]:
        """Return the raw delivery profiles, or raise ShopifyAuthError without the scope."""
        data = await self.graphql(DELIVERY_RATES_QUERY)
        return (data.get("deliveryProfiles") or {}).get("nodes") or []

    async def find_customers(self, query: str) -> List[Dict[str, Any]]:
        """Search customers with Shopify's own syntax. Matches loosely - verify results."""
        data = await self.graphql(CUSTOMERS_QUERY, {"query": query})
        return (data.get("customers") or {}).get("nodes") or []

    async def create_customer(self, customer: Dict[str, Any]) -> Dict[str, Any]:
        """Create one customer. Not retried: a repeat would make a second record."""
        data = await self.graphql(CUSTOMER_CREATE_MUTATION, {"input": customer},
                                  max_attempts=1)
        payload = data.get("customerCreate") or {}
        errors = payload.get("userErrors") or []
        if errors:
            message = "; ".join(str(item.get("message", item)) for item in errors)
            raise ShopifyRejected("Shopify refused the customer: " + message[:300], errors)
        created = payload.get("customer")
        if not created:
            raise ShopifyError("Shopify returned no customer and no error")
        return created

    async def ping(self) -> Dict[str, Any]:
        """Cheap call to prove the store and token work. Used by /health."""
        data = await self.graphql(SHOP_PING_QUERY)
        return data.get("shop") or {}


def _parse(response: httpx.Response) -> Dict[str, Any]:
    """Turn an HTTP response into GraphQL ``data``, mapping failures onto our errors."""
    status = response.status_code

    if status in (401, 403):
        raise ShopifyAuthError(
            "Shopify rejected the credentials (HTTP " + str(status) + "): "
            + response.text[:200]
        )
    if status == 429:
        raise ShopifyThrottled("Shopify rate limit (HTTP 429): " + response.text[:160])
    if status >= 500:
        raise ShopifyUnavailable("Shopify " + str(status) + ": " + response.text[:160])
    if status >= 400:
        raise ShopifyError("Shopify " + str(status) + ": " + response.text[:300])

    try:
        body = response.json()
    except ValueError as exc:
        raise ShopifyUnavailable("Shopify returned a non-JSON body: " + str(exc)) from exc

    errors = body.get("errors")
    if errors:
        # Shopify reports cost-based throttling inside a 200 response.
        codes = {
            (item.get("extensions") or {}).get("code")
            for item in errors
            if isinstance(item, dict)
        }
        message = "; ".join(
            str(item.get("message", item)) if isinstance(item, dict) else str(item)
            for item in errors
        )
        if "THROTTLED" in codes:
            raise ShopifyThrottled("Shopify throttled the query: " + message[:200])
        if "ACCESS_DENIED" in codes or "UNAUTHORIZED" in codes:
            raise ShopifyAuthError("Shopify denied access: " + message[:300])
        raise ShopifyError("Shopify GraphQL error: " + message[:400])

    data = body.get("data")
    if data is None:
        raise ShopifyError("Shopify returned no data: " + response.text[:200])

    _log_cost(body)
    return data


def _log_cost(body: Dict[str, Any]) -> None:
    """Record remaining query budget, so throttling is diagnosable from the logs."""
    cost = ((body.get("extensions") or {}).get("cost") or {})
    throttle = cost.get("throttleStatus") or {}
    available = throttle.get("currentlyAvailable")
    if available is not None and available < 400:
        logger.warning(
            "Shopify query budget low: %s/%s remaining",
            available, throttle.get("maximumAvailable"),
        )
