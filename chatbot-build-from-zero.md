# Wanas Gallery Chatbot — Full Build Plan (From Zero, Gemini-powered)

A Shopify-connected AI chatbot for Wanas Gallery, a premium/luxury clothing brand. This is a ground-up build — no prior codebase to reuse. Uses **Google Gemini's free API tier** as the LLM instead of a paid model, so cost stays at $0 during development/testing.

---

## 1. What the Bot Needs to Do

1. **Product search & Q&A** — answer questions about the catalog using real, live Shopify product data (not hallucinated)
2. **Image recognition** — a customer sends a photo (screenshot from elsewhere, or a photo of an item), and the bot identifies which catalog product(s) it most likely matches
3. **Full order-taking** — confirm product/size/color/quantity with the customer, then create the order in Shopify with two possible payment paths:
   - **Online payment** → create a draft order, send the customer a real Shopify checkout link to pay through (bot never touches card details)
   - **Cash on Delivery (COD)** → create a real Shopify Order directly (common in Egypt, where online payment adoption is lower)
4. **Order status lookup** — by order number or customer email/phone
5. **Customer support** — log complaints/issues as support tickets when the bot can't resolve something itself, and email the store owner
6. **Feedback collection** — record ratings/comments from customers
7. **Correct channel attribution** — orders created by the bot should show up in Shopify admin tagged as coming from "Chatbot", not lumped in with the online store

**Explicitly out of scope for this build:** WhatsApp/Instagram/Facebook integration. No Meta Business Provider is set up yet. Build and test everything against a simple local web chat interface first. Channel integrations come later as thin adapters on top of this — the core bot logic must not assume or depend on any particular channel.

---

## 2. Why Gemini (Free Tier) Instead of a Paid Model

- Google's Gemini API has a genuine free tier through Google AI Studio — no credit card required to start
- **Recommended model: `gemini-2.5-flash`** (or check Google AI Studio for the current best free-tier model at build time — this changes over time) — supports both function/tool calling and native image understanding, which this project needs for both order-taking (tools) and image recognition (vision) without needing separate APIs for each
- Free tier rate limits are modest (roughly 15 requests/minute, ~1,500/day as of mid-2026 — **verify current limits in Google AI Studio when building**, they change) — fine for development and light testing, but know this is a real constraint if testing extensively or eventually going live with real customer volume. Moving to a paid tier later is a small config change, not a rebuild.
- Get the API key at: https://aistudio.google.com — no billing setup needed for the free tier

---

## 3. Architecture: Modular Monolith

Single deployable FastAPI app (one process, one deployment) — but internally organized into clearly bounded modules by business domain, not a flat pile of scripts. Each module owns its own logic and data access; modules never reach into another module's internals directly, they only call each other's public **service** functions. This keeps things maintainable now and makes it realistic to split any module into its own service later without a rewrite, if that's ever needed.

```
app/
├── main.py                      # FastAPI app entrypoint — wires up routers, startup/shutdown
│
├── core/                        # shared infrastructure, not a business module
│   ├── config.py                 # loads/validates environment variables
│   └── database.py               # SQLAlchemy engine + session (SQLite locally / Postgres in prod)
│
├── integrations/                # low-level external API clients — no business logic
│   ├── shopify/
│   │   └── client.py             # raw Shopify Admin API calls (REST + GraphQL)
│   └── gemini/
│       └── client.py             # raw Gemini API wrapper (chat + vision + function calling)
│
├── modules/
│   ├── catalog/                  # product search + image-based product matching
│   │   ├── service.py             # search_products(), identify_product_from_image()
│   │   └── schemas.py             # Product, ProductMatch data shapes
│   │
│   ├── orders/                   # order creation + lookup
│   │   ├── service.py             # create_draft_order(), create_cod_order(), get_order_status()
│   │   └── schemas.py             # OrderRequest, OrderResult data shapes
│   │
│   ├── support/                  # support tickets
│   │   ├── service.py             # create_ticket()
│   │   ├── repository.py          # DB access for the tickets table
│   │   └── schemas.py
│   │
│   ├── feedback/                 # customer feedback/ratings
│   │   ├── service.py             # log_feedback()
│   │   ├── repository.py          # DB access for the feedback table
│   │   └── schemas.py
│   │
│   ├── notifications/            # outbound notifications (email now, more channels later)
│   │   └── service.py             # notify_new_ticket()
│   │
│   └── chat/                     # conversation orchestration — the "brain," composed of the above
│       ├── agent.py               # the Gemini agent loop: builds prompts, dispatches tool calls
│       ├── tools.py               # tool/function-schema definitions Gemini can call, each one
│       │                          #   thinly wraps a call into catalog/orders/support/feedback service
│       ├── router.py              # POST /chat FastAPI route
│       └── repository.py          # DB access for conversation history (messages table)
│
└── static/
    └── index.html                # local test chat widget (text + image upload)
```

### Module boundary rules (important — follow these strictly)

- **`modules/chat` is an orchestrator, not a business-logic owner.** It never talks to Shopify or the database directly for order/ticket/feedback data — it only calls `catalog.service`, `orders.service`, `support.service`, `feedback.service`. Its own `repository.py` is scoped *only* to conversation history, nothing else.
- **Each module's `repository.py` is the only code allowed to run SQL/ORM queries for that module's own tables.** No cross-module direct DB access — e.g. `orders` should never query the `tickets` table directly; if it needs ticket data, it calls `support.service`.
- **`integrations/` clients are dumb wrappers** — they know how to talk to Shopify/Gemini's APIs, but contain no business rules (e.g. `integrations/shopify/client.py` doesn't know what "COD" means — that logic lives in `modules/orders/service.py`, which calls the raw client).
- **`modules/chat/tools.py`** is the translation layer between "what Gemini is allowed to call" and "which module service actually does it" — keep these functions thin, just argument mapping + calling the right service.

This structure directly supports the build order in Section 8 below — each numbered step maps to building out one module (or one integration client) at a time.

---

## 4. Tools the Bot Needs (function/tool definitions for Gemini)

Defined in `modules/chat/tools.py`, each dispatching to the module service listed:

| Tool | Dispatches to | Purpose |
|---|---|---|
| `search_products` | `catalog.service.search_products()` | Look up products by keyword/category |
| `identify_product_from_image` | `catalog.service.identify_product_from_image()` | Given a customer's photo, find the closest matching catalog product(s) — see Section 6 |
| `get_order_status` | `orders.service.get_order_status()` | Look up an order by number |
| `get_orders_by_customer` | `orders.service.get_orders_by_customer()` | Look up recent orders by email or phone |
| `create_draft_order` | `orders.service.create_draft_order()` | Generate a checkout link for online payment |
| `create_cod_order` | `orders.service.create_cod_order()` | Create a real order directly for cash-on-delivery customers |
| `create_support_ticket` | `support.service.create_ticket()` | Log a complaint/issue for human follow-up — internally calls `notifications.service.notify_new_ticket()` |
| `log_feedback` | `feedback.service.log_feedback()` | Record a rating/comment |

The system prompt should make clear: **confirm order details with the customer before calling either order-creation tool** — never create an order the customer hasn't explicitly confirmed.

---

## 5. Cash on Delivery Order Details

Since COD orders are created directly (not via draft order + checkout), a few things need explicit handling:
- Mark the order's financial status appropriately (e.g. `pending`, since payment hasn't happened yet) so it doesn't look like a paid order in reporting
- Tag the order clearly (e.g. tag `"COD"`) so store staff can filter and find orders that need cash collected on delivery
- Collect a delivery address as part of the order flow — this isn't optional for COD since there's no Shopify checkout step to gather it. The bot needs to ask for the customer's name, phone, and address directly in conversation.
- Decide: does the bot validate/confirm the address format at all, or just pass through whatever the customer provides? (Recommend: pass through as given, but read it back to the customer for confirmation before finalizing the order, since typos in delivery addresses are costly.)

---

## 6. Image Recognition Approach

**Recommended approach for this build (v1, simple):**
- No separate embedding/vector database needed at this catalog size (~40 products)
- When a customer sends a photo, pass it directly to Gemini (which can view it natively) along with a compact text list of the product catalog (names + categories — not all product images, to keep the request light)
- Ask Gemini to identify the most likely matching product(s) or category based on visual characteristics described in its own reasoning, then use the existing `search_products` tool to pull 2-3 real candidates matching that guess
- Show the customer the candidates (with real product images/details) and ask them to confirm which one (if any) is right, rather than silently assuming a match
- If nothing seems like a good match, the bot should say so honestly and ask the customer to describe what they're looking for instead

**When to reconsider this approach:** if match accuracy turns out to be poor in practice once real testing happens, the next step up is precomputing image embeddings for every product photo (e.g. via a CLIP-style model) and doing a proper nearest-neighbor search before involving Gemini at all — this is more accurate at scale but meaningfully more engineering work, so it's not worth building preemptively.

---

## 7. Environment Variables Needed

```
# Gemini
GEMINI_API_KEY=

# Shopify
SHOPIFY_STORE=
SHOPIFY_ACCESS_TOKEN=

# Database (leave unset locally for SQLite; set in production for Postgres)
DATABASE_URL=

# Email notifications
SMTP_HOST=
SMTP_PORT=
SMTP_USER=
SMTP_PASS=
SMTP_FROM=
STORE_OWNER_EMAIL=
```

The Shopify token needs these scopes at minimum: `read_products, write_products, read_orders, write_orders, read_all_orders, read_draft_orders, write_draft_orders, read_customers, write_customers`.

---

## 8. Suggested Build Order (module by module)

1. **Scaffolding**: `core/config.py`, `core/database.py`, empty `main.py` that just runs — confirm the app boots
2. **`integrations/gemini/client.py`** + minimal `modules/chat` (agent.py, router.py, no tools yet) — confirm a basic Gemini text conversation works end-to-end through `POST /chat`
3. **`integrations/shopify/client.py`** (raw product search call) + **`modules/catalog`** (service wraps it) + register `search_products` in `modules/chat/tools.py` — confirm Gemini can call it and use real data in replies
4. **`modules/orders`**: `get_order_status()` / `get_orders_by_customer()` first (read-only, lower risk), wired as tools
5. Add image upload to `static/index.html` + `POST /chat` + confirm Gemini can view an uploaded image via `integrations/gemini/client.py`
6. **`catalog.service.identify_product_from_image()`** using the approach in Section 6, wired as a tool
7. **`orders.service.create_draft_order()`** (online payment path)
8. **`orders.service.create_cod_order()`** (Section 5) — including the address-collection conversation flow in the system prompt
9. **`modules/support`** + **`modules/notifications`** — ticket creation wired to email notification
10. **`modules/feedback`** — rating/comment logging
11. Verify in Shopify admin that bot-created orders show correct channel attribution (should happen automatically since orders are created via this app's API token — confirm in testing, investigate Shopify's Sales Channel APIs only if it doesn't show correctly by default)

At each step, confirm the module boundary rules from Section 3 are being followed before moving on — e.g. step 9 should not have any other module directly querying the tickets table.

---

## 9. Open Questions to Resolve During Build

- What exact financial_status/tags should COD orders use so staff can easily find them?
- Should there be a minimum confidence bar before the bot suggests a product match from an image, versus just saying "I'm not sure, can you tell me more"?
- What happens if Gemini's free tier rate limit is hit during testing — should there be a graceful fallback message, or is this acceptable to just fail during dev?
