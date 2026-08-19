# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Shopify-connected AI chatbot for Wanas Gallery (a premium Egyptian clothing brand),
built from scratch against the plan in `chatbot-build-from-zero.md`. That file is the
spec — read Section 3 (module boundaries), Section 8 (build order) and Section 9 (open
questions) before making structural changes. `README.md` records the decisions that were
made while building and *why*, including several that were reversed after live testing.

## Commands

```powershell
.\run.ps1                                                      # dev server (reload) on 127.0.0.1:8000
C:\Users\Fathy\anaconda3\envs\wanas\python.exe -m pytest -q     # full suite
... -m pytest tests/test_catalog.py -q                          # one file
... -m pytest tests/test_tools.py::test_declarations_are_plain_json_schema -q   # one test
```

`sh .restart.sh` restarts the server for manual testing: it frees port 8000 via
`netstat`/`taskkill` first (a plain kill leaves the listener behind and uvicorn then dies
with `[Errno 10048]`), waits for `/health`, and greps the startup log.

Endpoints: `/` (test chat widget), `/health` (which integrations are configured), `/docs`.

**Python must be the conda env `wanas` (3.12), never Anaconda base.** Base is 3.9.7 and
its `sqlite3` segfaults on `connect()` because `_sqlite3.pyd` binds a mismatched
`sqlite3.dll` from PATH.

Tests never touch the network and never spend Gemini quota: `conftest.py` gives each test
a throwaway SQLite file and clears the module-level LLM cooldowns.

## Architecture

One FastAPI process, internally a modular monolith. A request flows:

```
POST /chat → modules/chat/router.py → agent.handle_message()
                                        ├─ repository (conversation history only)
                                        ├─ integrations/llm.py → gemini/client.py
                                        └─ tools.dispatch() → <module>/service.py
```

**Boundary rules (from Section 3, restated in `app/modules/__init__.py`):**

- A module is reached only through its public `service.py`. The cross-module edges today
  are `chat/tools.py → catalog/orders/support/feedback`, `chat/agent.py → catalog +
  feedback`, `orders → catalog`, `support → notifications`, `feedback → notifications +
  orders`, and
  `notifications → orders + chat` (to enrich the ticket email). Keep the list short and
  always through `service.py` — `chat/service.py` exists only because `notifications`
  needed a legal way to read a transcript. `notifications` imports `support.schemas` and
  `feedback.schemas` rather than their services, which is what keeps those two edges from
  being cycles.
- A module's `repository.py` is the sole owner of that module's tables. No module queries
  another's tables — it calls that module's service instead.
- `integrations/` clients are dumb: they know an API's wire format and nothing about the
  business. `shopify/client.py` must never learn what "COD" means; that belongs in
  `modules/orders/service.py`.
- `chat/tools.py` is argument-mapping only — map, call one service, shape JSON. Logic
  accumulating there belongs in the owning module.
- `main.py` is wiring only. `core/config.py` is the only place that reads the environment.

### The LLM chain (`app/integrations/llm.py`)

Gemini-only, walking `[gemini_model] + gemini_fallback_models` until one answers. The
store currently runs a **single model** (`gemini-3.1-flash-lite`, empty fallback list) at
the owner's request — so the daily allowance is that one model's ~20 requests, and there
is no degraded-but-working path. `GEMINI_VISION_MODEL` can point image matching at a
different model; measured 1/3 vs 3/3 identification between the lite models. Free-tier
quota is **~20 requests per rolling window per model** (measured, not documented — the
published ~1,500/day figure is wrong), so the chain spans six models for headroom. A 429
is never retried on the same model; it puts that model in a cooldown for exactly the delay
the API asked for, and later turns skip it. Anything past the primary sets `degraded=True`,
which surfaces in the `/chat` response and the widget.

When the chain is exhausted the caller gets `LLMAllProvidersFailed` and the customer gets
a bilingual apology. **There is deliberately no third-party fallback.** An OpenRouter free
model was built and removed: it answered catalog questions without calling the search tool
and stated false stock levels roughly once in five. Do not re-add one without addressing
that. `llm_types.py` stays provider-neutral so a trustworthy provider could plug in.

Gemini quirks encoded in `gemini/client.py`, each one previously a live bug:

- `thinking_level` is 3.x-only — sending it to a 2.5 model knocks it out of the chain
  (`_supports_thinking_level()` gates it).
- 3.x function-call parts carry a `thought_signature` that **must** be replayed on the next
  request, so `ToolCall` carries `signature` and `_to_content()` reattaches it.
- Automatic function calling is disabled; `modules/chat/agent.py` runs the loop by hand.

### The catalog (`app/modules/catalog`)

Shopify Admin **GraphQL** (`2026-07`) — REST has no product keyword search and is legacy.
The whole catalog is cached for 5 minutes and matched locally, which buys tolerant
matching Shopify's query syntax cannot do: partial words (`cairoke`), Arabic garment and
colour vocabulary, diacritics and alef/ya variants. Nothing outside this module parses
Shopify product JSON.

Two tools exist on purpose: `search_products` is keyword-based and cannot see the whole
catalog, so it must never answer "cheapest" or "under X" questions — `browse_products`
does, over the cached catalog with no extra Shopify request.

**Price and available sizes belong to a colour, not to the product** — this store sells
the same t-shirt at 500 EGP in Burgundy (XL only) and 580 in Brown/Navy/Beige. The tool
payload therefore carries `available`: one row per in-stock colour with its own price and
sizes. Never flatten that back into separate colour and size lists — it implies
combinations that do not exist, which is exactly the bug it replaced.

Guards: non-`ACTIVE` products are dropped; only in-stock sizes/colours reach the model; a
Shopify failure serves the stale cache, and with no cache raises rather than returning an
empty list ("we have nothing" would be a lie). `online_url` is `null` for every product
while the storefront is password-protected, so the bot offers no links — publishing the
store fixes that with no code change. `storefront_is_open()` exposes the same fact
synchronously from the cache, and `build_system_prompt()` uses it to forbid sending
customers to the shop to order or pay while it is shut (it once told a customer to
"complete your order through our online store"). Unknown counts as closed.

### Orders (`app/modules/orders`)

Read-only so far. The rule that shapes this module: **an order number alone never opens
an order.** They are sequential and guessable, and the record holds a name, phone,
delivery city and purchase history, so `get_order_status` also requires the email or
phone on the order (`orders_require_contact_verification`, on by default).

That check runs in `service.py` and must stay there, because **Shopify's search cannot
do it** — `phone:01067177129` returns orders belonging to `+201000000000`. Its filters
rank by relevance, not equality, so every result is re-verified locally, including the
order number itself (`name:#100` matches `#1003`). Phones compare on their last ten
digits, which unifies `+20`, `0020`, `0` and Arabic-Indic spellings; shorter input never
matches, so a fragment cannot act as a wildcard. A wrong contact and a non-existent
order return an identical `not_found`, so the tool cannot enumerate real order numbers.
`schemas.to_tool_dict()` is the privacy boundary — no street address, phone, email,
internal note, staff tags or Shopify id reaches the model.

### Image uploads (`app/modules/chat/attachments.py`)

`POST /chat` takes base64 images in the JSON body (bare or as a data URL) rather than a
multipart upload — one request shape for every future channel, and testable with a plain
JSON client. Text or image alone is a valid turn.

**The declared content type is never trusted** — bytes are sniffed and the sniffed type
wins, because browsers mislabel files and iPhones report HEIC as JPEG. HEIC/HEIF must
stay supported for that reason. Oversized payloads are rejected on encoded length before
being decoded. `AttachmentError` messages are customer-facing and pass straight through
the route as the 422 detail.

Image bytes are never persisted: history stores `[image]`/`[N images]`, and a photo rides
only on the turn it arrived with. The prompt tells the model to ask for it again rather
than recall it.

**Photo matching runs through `identify_product_from_image`, never `search_products`.**
The catalog service shows the photo to a model alongside a compact text index of the real
catalog and asks which of those it could be (no embeddings — eighteen products).

The owner chose *assert when confident, ask otherwise*, so `confident` is computed in
`_verify()`, never taken from the model: it requires a self-reported "high", a product
that actually exists, a claimed colour that is a real option on it, and no equally-rated
runner-up. Failing any check still returns the candidates with `confident: false`, and
the prompt then requires the bot to ask rather than pick. Do not loosen these checks
without re-measuring the false-assert rate — before this existed, the bot searched on its
own description of a photo and announced "That is our Ringer Boxy Fit T-shirt".

`identify_product_from_image` is the one tool that receives dispatch context: images are
passed to `tools.dispatch(..., images=...)` and only handlers marked `wants_images` get
them, since a photo cannot travel as a model-supplied argument.

### Creating orders (COD)

`create_cod_order()` creates a **real** order — the first irreversible thing the bot does.
Convention (owner-confirmed): `PENDING` + tags `cash-on-delivery`, `chatbot`, and the
channel. The channel comes from `ToolContext`, never from model arguments, or a web order
would eventually be tagged `whatsapp` and misfiled.

Load-bearing guards, none of which should be relaxed casually:

- Variants are re-resolved from the live catalog at creation time; the caller's claim is
  not trusted, so a sold-out size cannot be ordered.
- Only `variantId` + `quantity` are sent — **never a price**.
- `DECREMENT_OBEYING_POLICY` rather than `BYPASS`, so an order fails instead of overselling.
- An identical order for the same phone within `cod_duplicate_window_seconds` returns the
  existing order. Checked against Shopify, not memory, so it survives restarts.
- `create_order` is the one Shopify call that is **not retried** — an ambiguous failure
  must not become two orders.
- **Four fields `orderCreate` will not infer**, each found missing on a real order:
  `provinceCode` (via `orders/governorates.py`, 29 Egyptian governorates matched from
  Arabic or English), `requiresShipping: true` per line (it defaults to **false** even for
  physical goods), `customer.toUpsert` (or the order reads "No customer"), and
  `shippingLines` (only when `COD_SHIPPING_FEE` is set — never invent a delivery charge).
- **Phones must be E.164.** Shopify rejects `01000000000` with "Phone is invalid";
  `to_e164()` converts, the staff note keeps the customer's spelling.

The read-back-and-confirm rule lives only in the prompt — `customer_confirmed` is filled
in by the model and proves nothing on its own.

`ToolContext` carries what the model must not choose (attached images, channel,
conversation id); only handlers marked `wants_context` receive it.

### Telling a customer their order shipped

Same shape as the feedback trigger, and the same limitation: **the bot cannot start a
conversation**, so the news rides on the customer's next message. When WhatsApp exists,
`orders.service.shipping_news()` is the detection half already built and tested — only
the delivery changes.

`conversation_orders.shipped_told_at` is the "said once" marker. It lives on that table
rather than in `notifications` because it is a fact about this order in this conversation,
not about a channel.

What is deliberately excluded from the news:

- **cancelled** orders;
- orders that have **already reached the customer** — telling someone their parcel is on
  its way while they are holding it is worse than saying nothing. These are marked told on
  the way past, so they stop costing a Shopify lookup every turn.

The marker is set in `agent.handle_message()` **after** the reply is persisted, not when
the prompt is built. A turn that fails tells them next time; announcing into a dropped
request would lose the news entirely. The trade is that a model which ignores the note
burns the announcement — the same risk the feedback ask carries.

The prompt allows only what Shopify actually knows: the parcel left the shop. It forbids
saying where it is now, what stage it is at, or when it will land — timing comes from the
`delivery_period` rule, as a range and never a date. Tracking is included when Shopify has
it.

### How long delivery takes

**Shopify has no field for it.** Verified 2026-08-19 against the live store: one Domestic
zone covering all 29 governorates, one rate named `قياسي`, `description` null. The
`shopPolicies` field exists but the token lacks `read_legal_policies`, and a policy page
is prose anyway. So the period is configuration — `delivery_days_min`/`max` — and the
owner's answer was that it is the same everywhere (3–5 working days).

`orders.service.delivery_period()` returns `None` rather than a guess when unset, and both
`create_cod_order` and `get_delivery_cost` attach `delivery_period` to their result only
when it is known. The prompt allows a delivery time **only** from that field, still forbids
inventing a date, and forbids turning the range into a calendar day. The original
"never estimate a delivery date" rule survives — this is a narrow, sourced exception to it.

### Cancelling an order (`orders.service.cancel_order`)

The second irreversible thing the bot does, and the rule is `policy.md` verbatim:
**cancellation is allowed before the order ships; after that an exchange applies.**
`cancellable()` is separate from the act so the answer can be given before anything is
promised, and it returns a bare reason code the prompt maps to words.

Three things stop it, and none should be relaxed:

- **already_shipped** — `FULFILLED` or `PARTIALLY_FULFILLED`. The policy line itself.
- **already_paid** — `PAID`/`PARTIALLY_PAID`/`PARTIALLY_REFUNDED`. An unshipped COD order
  is money that never moved; anything else means the bot would be cancelling its way into
  owing a refund, which is a person's decision.
- **the contact check** — `cancel_order()` goes through `get_order_status()`, not around
  it. Cancelling must never become a way past the guard that stops guessable order
  numbers being enumerated, so "no such order" and "not yours" stay indistinguishable.

`refund=False, restock=True`: nothing was paid, and without the restock the stock stays
held for an order that no longer exists. `notifyCustomer=False` — they are being told in
the chat. Like `create_order` it is **not retried**.

**Shopify cancels in a background job**, so the mutation returning cleanly means
*accepted*, not *done*. The order is re-read afterwards and the tool reports
`confirmed: order.is_cancelled` — measured live, a cancel is usually still `False` at that
moment, and the prompt requires "it is being cancelled now" rather than "it is cancelled".
A second cancel comes back as `shopify_refused` rather than `already_cancelled` for the
same reason, so the prompt carries a catch-all: any unnamed error means it did not happen.

The owner is emailed every time (`notifications.notify_order_cancelled`) — their decision.
Best-effort as always: the order is already cancelled, and a mail failure must not make
the bot claim otherwise.

**Exchanges are not automated and must not be.** `policy.md` makes them a courier-and-human
process — 24 hours, original packaging, unworn, and who pays depends on why. The prompt
states the terms and files a `return_or_exchange` ticket; it is forbidden to waive a fee or
promise an exchange will be accepted.

### Support tickets (`app/modules/support`, `app/modules/notifications`)

`support` owns the `support_tickets` table; `notifications` owns telling someone. They are
separate modules for one reason: **the ticket is stored first and the email is
best-effort**, so a dead mail server never loses a complaint. Nothing in
`notifications/service.py` raises — it returns False and logs. `smtplib` is blocking, so
delivery runs in a worker thread.

Without SMTP configured the ticket is still stored and the customer still gets a
reference; a WARNING says nobody was emailed. References avoid O/0, I/1, S/5, B/8 —
customers read them aloud. Same category + same conversation within
`ticket_duplicate_window_seconds` returns the existing ticket rather than filing again.

The email carries more than the ticket row: the **order as it stands right now** (status,
payment, items with variants, totals split into goods and delivery, the contact and city
on the order, tracking, and an `admin.shopify.com` deep link) and the **conversation
transcript** behind the model's summary — the summary is the model's own account of a
chat it also conducted, so the evidence travels with it. Both are gathered *defensively*,
after the ticket is stored: `_order_for()` and `_transcript_for()` swallow and log, so
Shopify being down costs the order block, not the email.

`orders.service.lookup_for_staff()` exists for this and **must never become a tool** — it
skips the contact check that stops guessable order numbers being enumerated, which is safe
only because the recipient is the store owner. A mismatch between the ticket's contact and
the order's is *reported in the body* rather than hiding the order: someone quoting an
order they cannot prove is theirs is exactly what a human should see. A test asserts no
declared tool name contains "staff".

### Feedback (`app/modules/feedback`)

An opinion, not a job. `support` is for something a customer wants *done*; `feedback` is
for what they think. Filing praise as a ticket clogs the queue a person works through,
and filing a complaint as feedback buries it — the tool descriptions draw that line
explicitly ("if they want an outcome, it is a ticket").

Two owner decisions (2026-08-19) shape it, and both are asserted in `tests/test_feedback.py`:

- **No score is ever asked for.** No stars, no 1–5, no out-of-ten. Customers say what they
  think in their own words, stored verbatim — not translated, not summarised. The
  `sentiment` stored beside the comment is read off what they wrote, never requested from
  them. The bot asks once, open-ended, right after placing an order, and drops it if
  they would rather not answer.
- **Only negative feedback emails the owner.** Everything is stored; praise does not fill
  the inbox. The branch lives in `feedback/service.py`, not in `notifications` — the
  notifier does not second-guess whether it should have been called.

`schemas.DEFAULT_SENTIMENT` is `"negative"` on purpose: an unrecognised or missing
sentiment falls *towards* a person. A complaint silently filed as fine is a complaint
lost, and an unnecessary email costs nothing.

Feedback carries **no customer-facing reference**, deliberately unlike a ticket. Nobody
chases feedback up, and handing someone a code invites them to expect a reply that is not
coming. `to_tool_dict()` returns only `{"recorded": true, "sentiment": ...}` — no contact
details are read back.

One conversation is one opinion (`feedback_duplicate_window_seconds`), so a customer who
says thank you twice is recorded once.

**The bot never asks on its own initiative, and never at order time** (owner's call,
2026-08-19: feedback is about the goods, not the chat). Asking someone how a garment is
before it reaches them is asking about something they have not seen. Instead:

- `create_cod_order` succeeding writes a `feedback_requests` row — this conversation is
  owed feedback on that order, *later*.
- `agent.handle_message()` calls `feedback.service.review_due()` once per turn. That
  re-reads the order from Shopify (never cached — the whole question is whether its state
  changed) and returns it only when `Order.reached_the_customer` is true.
- If it is, `build_system_prompt()` appends a note naming **the actual pieces**, because
  "how was your order?" gets nothing and "how was the Ringer in Brown?" gets an answer.

`Order.reached_the_customer` is deliberately strict, and COD-only: the customer pays the
courier at the door, so `PAID` cannot precede arrival. `FULFILLED` is not enough — it only
means the parcel left the shop. Prepaid orders return False rather than a guess, since
they are `PAID` at checkout; revisit when step 7 lands.

`review_due()` never raises: it runs on the way into an ordinary customer turn, so Shopify
being down costs the feedback prompt, not the conversation. Recording feedback or calling
`close_review()` closes the row, so nobody is asked twice.

This adds `feedback → orders` and `chat/agent.py → feedback` to the edge list. `feedback`
uses `lookup_for_staff()` for the same reason `notifications` does — the conversation
created the order, so it is entitled to it, and there is no customer contact to check.

### Prompt and tool-result conventions (`app/modules/chat/agent.py`)

- **Tool results carry data only, never instructions.** A result once contained
  `"instruction": "Tell the customer…"` and the model read it aloud. Errors are bare codes
  like `{"error": "catalog_unavailable"}`; how to behave is set in the system prompt. A
  regression test in `tests/test_tools.py` asserts no directive text appears in results.
- Language and tone: **everyday Egyptian Arabic (العامية المصرية)** for anyone writing
  Arabic — including formal Arabic and Arabizi — and friendly, natural English for
  English. This reverses the original Modern Standard Arabic rule at the owner's request
  (2026-08-18): the shop should sound like a person in the shop, not a news bulletin.
  The prompt lists the colloquial forms to use (ده، دي، دلوقتي، عايز) and the written
  forms to avoid (هذا، هذه، الآن، سوف), because a lite model drifts back to الفصحى
  without concrete examples. Tone changes how it sounds, never what it says — the
  honesty rules are stated as outranking it.
- Plain text only — the widget renders replies verbatim, so no markdown.
- `_NOT_YET_BUILT` lists capabilities that don't exist yet; each build step deletes a line.
- When `max_tool_rounds` is exhausted the agent asks once more with **no tools**, forcing a
  worded answer instead of an apology after a search loop.
- Only the final text is written to history; tool calls and results stay inside the turn.
- **The language rule is restated at the very end of the prompt.** An English complaint
  once got an Arabic reply because LANGUAGE sits at the top of a 10k-character prompt and
  was lost by the end. A test asserts the restatement stays in the final fifth. As the
  prompt grows, treat position as load-bearing.

## Working agreement

Build in the order of Section 8, one numbered step at a time, and **confirm with the user
before starting the next step**. Don't collapse modules together, and keep the boundaries
intact from the start. Progress is tracked in the README checklist (steps 1–6 and 8–10 done). Step 7,
`create_draft_order()`, is deferred until the storefront password is lifted; step 11
(verifying channel attribution in the Shopify admin) is still unchecked.

Both Section 9 business questions are now answered by the user: image matching asserts
when confident and asks otherwise; COD orders use `PENDING` with tags `cash-on-delivery`,
`chatbot` and the channel. Keep asking rather than assuming on anything comparable —
these were the user's calls to make, not defaults to pick.

Out of scope by explicit instruction: **no WhatsApp/Instagram/Facebook integration.** No
Meta Business Provider exists. Everything is built and tested against `app/static/index.html`.
