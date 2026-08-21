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

### Database engine (`app/core/database.py`, `app/core/config.py`)

SQLite locally (unset `DATABASE_URL`, `./data/wanas.db`), Postgres in production
(Railway's provisioned database). `sqlalchemy_url` normalises whatever scheme the
platform hands out — `postgres://` or `postgresql://`, neither naming a driver — to
`postgresql+psycopg://`, so a Railway-generated `DATABASE_URL` works unedited. Every
`_now()` helper across the modules already returns `datetime.now(timezone.utc)`, so the
date-range queries (`admin.analytics`, `conversation_count_in_range`, etc.) compare
tz-aware Python values against `DateTime(timezone=True)` columns on both dialects —
verified live against a throwaway Postgres container, not just inferred.

`_add_missing_columns()` (the informal migration patcher described below) is **SQLite
only**, gated on `dialect.name`. It builds `ALTER TABLE` DDL by hand, including a
`DEFAULT 0` for booleans that Postgres rejects (it wants `FALSE`), and there is nothing
for it to patch on Postgres anyway — every Postgres environment this project points at
starts from an empty database, so `create_all()` alone gives it every column already.
Don't make this function bilingual; if Postgres ever needs real migrations, that is
Alembic's job, not this one's.

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
  `notifications → orders + chat` (to enrich the ticket email). Instagram adds
  `engagement → chat + catalog + support`, all through `service.py`;
  `chat/service.py` grew `handle_message()` so a channel adapter never touches
  `agent.py`. Keep the list short and
  always through `service.py` — `chat/service.py` began as the legal way for
  `notifications` to read a transcript, and is now also how every non-web channel holds
  a turn. `notifications` imports `support.schemas` and
  `feedback.schemas` rather than their services, which is what keeps those two edges from
  being cycles. The owner dashboard adds `admin.analytics → orders + chat + support +
  feedback + engagement` and `admin.conversations → chat + orders + support + feedback +
  engagement`; every edge from `admin.conversations` is read-only except one write path,
  Instagram reply/takeover, which goes through `chat.service.post_owner_message()` /
  `resume_bot()` and `engagement.service.send_owner_reply()` — never around them.
  `chat.service` and `chat.repository` gained channel-filtered and
  date-ranged variants of what they already exposed (`conversations(limit, channel)`,
  `conversation_count_in_range()`, `inbound_timestamps_in_range()`, `get_conversation()`)
  rather than `admin` reaching into `chat`'s tables.
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
being decoded. For audio the sniffing carries more weight still: Gemini works out what
the bytes are and ignores the label entirely (measured 2026-08-19, one WAV was accepted
as `audio/wav`, `audio/webm` and `audio/ogg` alike), so this file is the only thing
standing between a stranger's upload and the model.

`AttachmentError` messages are customer-facing and pass straight through
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

### Voice notes (`app/modules/chat/voice.py`)

Customers here speak more readily than they type. A voice note is **transcribed first**,
and the transcript is what everything else sees — the assistant answers text, the history
stores text, a ticket quotes text. That costs one extra model request per spoken turn and
buys the thing a photo does not need: **the words survive the turn.** A photo can be
forgotten because the photo is rarely the message; a voice note *is* the message, and
"the one I told you about" has to still mean something next turn. It also means the
owner's dashboard shows what was said rather than a row of `[voice note]`.

Transcription runs on its own model (`GEMINI_TRANSCRIPTION_MODEL`) for the reason vision
does: each model carries its own free-tier budget, so listening does not eat the
allowance for replying. Stored history is prefixed `[voice]` so a reader knows a message
was heard rather than typed — mishearings read very differently once you know.

**Silence is caught in code, not by the model** (`attachments.carries_sound`). Measured
2026-08-19: asked to transcribe one second of digital silence, `gemini-3.7-flash`
invented a plausible Egyptian sentence **three times out of three**, and the assistant
then answered a message the customer never sent. The system prompt forbidding exactly
that did not hold. So the samples are measured before a request is ever spent: peak
amplitude under 0.8% of full scale over essentially the whole clip is silence, and the
customer is told the microphone picked nothing up. Only PCM WAV can be measured without
decoding — anything compressed gets the benefit of the doubt rather than a guess.

That measurement also chose the model. Against a real Egyptian voice note, **every**
model transcribed the phone number correctly, so accuracy did not decide it —
confabulation did, and the newer flash models are worse at it, not better:

| model | transcript | invented speech on empty audio |
|---|---|---|
| `gemini-2.5-flash` | exact | 0/2 |
| `gemini-3.1-flash-lite` | exact | 0/2 |
| `gemini-3.5-flash-lite` | one letter dropped | 0/2 |
| `gemini-3.5-flash` / `3.6-flash` | exact | 1/2 |
| `gemini-3.7-flash` | — | 3/3 |

**A transcript is never evidence.** Speech gets misheard, and the thing an Egyptian
customer is most likely to say aloud is a phone number. The prompt requires anything
spoken that will reach an order to be read back first, phone numbers digit by digit. The
`/chat` response also returns `transcript`, and the widget replaces the customer's own
bubble with it — a mishearing is obvious to the one person who can spot it.

**The widget converts whatever the browser recorded to 16 kHz mono WAV** before sending
(`app/static/index.html`). Chrome records WebM/Opus and Safari MP4/AAC; converting in the
page means one format server-side instead of three, and the resample is done by an
`OfflineAudioContext` rather than by hand, because naive decimation aliases and aliasing
reads as words the customer never said. `decode_audio` still accepts ogg/mp3/m4a/aac/flac,
which is what a WhatsApp adapter will hand over.

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

### Paying online (`orders.service.create_draft_order`)

The other half of the payment story, and the one thing that keeps it safe: **the bot
never touches a payment.** It creates a Shopify *draft order* — a priced basket with a
checkout link — and hands the link over. Nothing exists in the shop until the customer
pays through it. There is no code path that could accept a card number, which is why the
prompt can flatly refuse one.

**A draft's `invoiceUrl` reaches the checkout even while the storefront is
password-protected.** Verified live 2026-08-19: it lands on `/checkouts/do/…` with HTTP
200, not the password page. That single fact is what lets this ship before the shop is
published, and `_STOREFRONT_CLOSED` carries an explicit carve-out for it — without that,
the "never send them to the website" rule would suppress the link.

`Draft` is a separate type from `Order` on purpose: code holding a `Draft` cannot
accidentally tell a customer their order is placed. Its `to_tool_dict()` calls the amount
**`items_total`, not `total`** — a draft is priced before a delivery address exists, so
it is the goods only, and the earlier name invited the bot to read it out as the amount
they would pay. The Shopify id and the draft's own name (`#D3`) never reach the model;
that name is not an order number and staff cannot look one up by it.

Guards, mirroring the COD path where the risk is the same:

- Variants are re-resolved from the live catalog, so a link is never issued for a
  sold-out size. Only `variantId` + `quantity` are sent — **never a price**.
- `requiresShipping: true` per line, for the same reason `orderCreate` needs it.
- **One basket, one link.** A repeat call reuses the link it already gave. Unlike the COD
  duplicate check this has **no time window**, deliberately: an old link still takes
  money, so expiring it would leave two live links for one basket and a customer who pays
  both has paid twice. What ends the reuse is the price — a draft holds the price it was
  created at, so once the catalog disagrees (`_same_money`) a fresh link is made.
- Tagged `online-payment` + `chatbot` + the channel, and **never `cash-on-delivery`** —
  that tag means cash to collect. `_staff_note` says "Paid online" for the same reason:
  a note claiming COD on a paid order gets money asked for at the door.
- No address is required. The checkout collects and validates it, and asking twice loses
  customers between the two. Anything the customer already volunteered is passed through,
  but a half-address is sent as none — a partly filled checkout reads as complete and
  gets clicked past.

**Finding out that they paid.** The bot cannot see a payment happen, so it must never
claim one. `payment_news(conversation_id)` runs once per customer turn, like the feedback
and shipping checks and with the same never-raises contract. It re-reads each outstanding
draft; a draft that has become an order is settled with that order's number and **linked
into `conversation_orders` as an ordinary order of this conversation**, which is what
makes the shipping notice, the feedback ask and the dashboard pick it up with no extra
work. A draft deleted in the admin is settled with no order, so it stops being polled.
`agent.handle_message()` calls `feedback_service.expect_review()` for each newly-paid
order — in the agent, not in `orders`, because `feedback → orders` already exists and the
reverse edge would be a cycle. The "told" marker is set after the reply is persisted,
the same trade as the shipping notice.

The tool is `create_payment_link`, and it is **not declared at all** when
`online_payment_configured` is false, so the model cannot offer a way to pay the store
does not have. `_available()` in `tools.py` is the general mechanism; `dispatch()`
re-checks it, so a call from a stale prompt is refused rather than run.

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

`Order.reached_the_customer` is deliberately strict. For COD it is exact: the customer
pays the courier at the door, so `PAID` cannot precede arrival. `FULFILLED` is not
enough — it only means the parcel left the shop. For an order **paid online** `PAID`
happens at checkout and says nothing about delivery, so the only fact left is the
carrier's own report (`deliveredAt` / `displayStatus: DELIVERED`), which Shopify carries
but which stays empty unless the courier integrates with the shop — it is empty for this
store today. A prepaid order therefore never counts as arrived, and nobody is asked how
it was. That is the intended failure: silence rather than asking someone about a parcel
they may not be holding.

`review_due()` never raises: it runs on the way into an ordinary customer turn, so Shopify
being down costs the feedback prompt, not the conversation. Recording feedback or calling
`close_review()` closes the row, so nobody is asked twice.

This adds `feedback → orders` and `chat/agent.py → feedback` to the edge list. `feedback`
uses `lookup_for_staff()` for the same reason `notifications` does — the conversation
created the order, so it is entitled to it, and there is no customer contact to check.

### Instagram: comments and DMs (`app/modules/engagement`)

The first channel other than the widget. A comment on a post is triaged; an important one
opens a DM, and from there it is an ordinary conversation - the catalog, COD orders,
payment links, photo matching and voice notes all work with no channel-specific code,
because a DM goes through `chat.service.handle_message()`, the same door the widget's
route reaches through `agent.py`. **Nothing here re-implements the assistant.** An
adapter that grew its own answers would be a second bot with a second set of honesty
rules.

Four rules, each of which is the reason some part of this is shaped the way it is:

- **Nothing a model writes is ever posted in public.** The public reply and the DM opener
  are fixed templates, in Arabic or English chosen from the comment's own script. A
  comment reply is permanent and read by everyone; the failure mode of a model going
  slightly off-script there is far worse than in a private chat. It also costs no quota.
  The bot cannot open a conversation anyway - `handle_message()` answers a customer turn.
- **Silence is the safe failure.** A classifier that cannot be reached, a catalog that is
  down, an unreadable answer - all of them mean *nothing is posted, liked or sent*. An
  unanswered comment is where the shop was before any of this existed.
- **Nothing outward happens twice.** `repository.claim()` writes the event id before the
  work starts, because Meta redelivers anything it did not get a fast 200 for. That
  matters most for the private reply: Meta allows **one per comment for all time**
  (subcode 2534014), so a retry does not resend it, it spends it.
- **The dry run is real.** `INSTAGRAM_DRY_RUN` (**on by default**) runs every path,
  decides everything, and logs instead of calling. It is how a live run gets read before
  it is published.

Ordering that is load-bearing: **the DM is sent before the public reply.** The public
reply says "we have sent you a DM", so it must not appear if the DM failed. A DM with no
public acknowledgement is merely quiet; the other way round is the shop saying something
untrue where everyone can read it.

**The two loops.** Our own public reply arrives back as a comment webhook, and Meta echoes
our own DMs on the messaging webhook. Both are dropped in `accept()` by author id and
`is_echo`. Without them the shop talks to itself until the rate limiter stops it.

**Joining a comment to the DM thread.** A comment carries a different id for a person than
a message does, so they could not be matched from the comment alone. The private reply's
own response carries `recipient_id` - the messaging id - so the thread is keyed on it the
moment the DM goes out, and the customer's reply lands in the conversation that already
holds their comment and the opener. If Meta ever omits it, that is logged loudly: the
customer would otherwise be asked things they have already answered.

**Which product a post is about** (`resolve_post_product`). Instagram says who commented
and on which post, never which product the post shows. Caption first, because keyword
matching against the cached catalog is free; only if that is inconclusive is the post's
image sent to `catalog.identify_product_from_image()` - the same function and the same
*earned* confidence rules a customer's photo goes through. Resolved or not, the answer is
cached against the `media_id`, so a post pays once rather than once per comment. An
unmatched post stays unmatched and the bot asks which piece they meant. **A cached match
answers which product and never what it costs** - price and stock are always a live tool
call, which is why the opener names a piece and carries no number.

**Quota shapes the classifier.** Measured 2026-08-20 while testing this: the free tier
is **15 requests per minute per model** (`GenerateRequestsPerMinutePerProjectPerModel-FreeTier`),
which is a sharper limit than the ~20-per-rolling-window figure recorded earlier -
classifying fourteen comments emptied the chat model's allowance in seconds, and every
comment after that was silently left alone. Comments arrive whether or not a customer is
mid-conversation, so the classifier must not be able to starve the assistant. So the free
rules run first - a comment that is only an @mention is "neither" (the owner's call:
tagging a friend is pointing, not asking), a row of hearts is "positive" - and only real
words reach `GEMINI_CLASSIFIER_MODEL` (`gemini-3.5-flash`), which has its own budget for
the same reason vision and transcription do. It judged 14 of 14 real Egyptian comments
the way a person would, including Arabizi - and read "terrible service, I have been
waiting two weeks" as *important* rather than negative, which is right: they want
something done, so it belongs in a DM and not in a silent ticket. `InstagramClient` also self-throttles to
`INSTAGRAM_MAX_ACTIONS_PER_HOUR` (200), well under Meta's ceiling: a runaway loop is
cheaper to notice at 200 than at 750.

**Negative comments file a support ticket** (the owner's decision) with
`contact="instagram:@handle"`, which is truthful and satisfies the contact guard without
relaxing it. Category `complaint` rather than a new one, because `CATEGORIES` is also the
enum the chat model picks from and a web customer must not be offered "public comment".
No public reply, no like, no DM.

**The webhook is signed.** `router.py` verifies `X-Hub-Signature-256` over the *raw* body
and fails closed when no app secret is set. Without it anyone who found the URL could post
a fabricated comment and have the bot reply to it in public. Everything else answers 200
even when it does nothing, because Meta disables a subscription that keeps erroring - and
a disabled subscription is silent in exactly the way nobody notices.

**Writes are never retried** in `integrations/instagram/client.py`: none of Meta's send
endpoints are idempotent. Reads retry with backoff as Shopify's do.

Attachments: a DM photo or voice note arrives as a URL, is downloaded, and goes through
`chat/attachments.py` unchanged - the same sniffing, the same size limits, the same
silence gate. `attachments.from_bytes()` exists so there is one validation path rather
than two that drift. Meta's *structural* kind (video, share, story) is trusted enough to
skip the download, because an mp4 sniffs as an audio container and "that recording is too
short to hear" is a baffling thing to hear back about a video.

### The owner dashboard (`app/modules/admin`, `dashboard/`)

An internal tool, not a customer-facing feature — spec is `owner-dashboard-plan.md`.
Three sub-modules (`auth`, `analytics`, `conversations`) behind `/admin/api/*`, plus a
separate React/TypeScript app in `dashboard/` that FastAPI serves at `/admin` once built
(`npm run build`; `dashboard/README.md` has the day-to-day details). Same-origin by
design — Section 6 chose "one platform, no cross-origin complexity" over CORS, so the
built frontend is static files this app mounts, not a second deployed service.

**`admin.auth`** owns two tables (`admin_accounts`, `admin_sessions`) and is the only
code allowed to touch them. Passwords are `hashlib.scrypt` (stdlib — no new dependency),
sessions are a random bearer token of which only the SHA-256 is ever stored, and a
wrong username costs the same wall-clock time as a wrong password (a dummy hash is run
either way) so login cannot be used to enumerate usernames. `require_owner_account` is
the one place "owner-only" is enforced, reused by every other admin router rather than
each one checking the role itself.

**`ADMIN_OWNER_PASSWORD` always wins, on every restart** (owner's call, 2026-08-20).
`bootstrap_owner()` doesn't just create the account once — it re-syncs the password hash
to match the environment variable on every startup. That was a deliberate reversal: the
first version created the account once and then ignored the variable forever, which
does not match "I control who logs in via variables". There is deliberately no
dashboard-side change-password screen; changing the password is edit-`.env`-and-restart.
A same-named `staff` account is left alone rather than silently promoted to owner.

**`admin.analytics`** computes KPIs by calling other modules' `service.py` — never SQL,
never a Shopify call, of its own. Two v1 KPIs from the plan don't honestly exist and say
so rather than faking a number: `resolution_tracking_available: false` (nothing in this
app ever moves a support ticket past `STATUS_OPEN` — there is no resolution workflow
built) and `rating_available: false` (feedback is deliberately never scored — see
`modules/feedback`). `orders.service.orders_in_range()` re-verifies Shopify's own
`tag:`/`created_at:` search locally before trusting it, the same defensive habit
`get_order_status` uses for contact matching — Shopify's search has been wrong before.
`Snapshot.daily` (one `{date, orders, revenue}` row per calendar day, zero-filled) feeds
the revenue chart; it was missing from the first cut and only surfaced once the frontend
needed a time series rather than a period total.

**Channels the dashboard shows are not the channels the chatbot has.** `web` and
`instagram` are real; `whatsapp`, `tiktok` and `facebook` come back
`connected: false` with zero data rather than a real-but-empty snapshot — matching
`CLAUDE.md`'s own confirmed scope (no WhatsApp integration exists) rather than the
plan's literal five-tab list. `"all"` (`channel=None` internally) aggregates **every**
chatbot-tagged order regardless of channel, including a handful of real `whatsapp`-
tagged orders from before this chatbot existed — so "all" can be larger than
`web + instagram` combined, honestly, and that gap has no tab of its own to explain it.

**`admin.conversations` is read-only for every channel except one write path: Instagram
reply/takeover** (owner's call, 2026-08-20). It replaces the older, single-shared-token
`modules/dashboard` view, removed 2026-08-20 once this module and the React frontend
covered everything it did — `DASHBOARD_TOKEN` and the `/dashboard` route no longer exist.
**`"all" shows analytics only, never a merged
conversation list`** (owner's call, 2026-08-20) — the backend's
`/admin/api/conversations/all` works and is tested, but `dashboard/src/pages/Dashboard.tsx`
never calls it; mixing every channel's conversations into one list wasn't wanted. Asking
for a real conversation under the wrong channel's URL is a 404, not a cross-channel leak
— `chat.service.get_conversation()` exists because `transcript()` alone can't tell "no
such conversation" apart from "a real one with no messages yet", and both look like an
empty list otherwise.

**Every conversation's title is a resolved `customer_name`, never blank.** Nothing
stores a customer's name directly; `admin/conversations/service.py`'s `_customer_name()`
tries the Instagram handle (`engagement.service.username_for_conversation()`), then a
support ticket's or feedback's `customer_name`, then falls back to `"عميل " +
conversation_id[:8]`. Resolved once, backend-side, so the list row and the detail
header can never disagree and the frontend carries no fallback logic of its own.

**The owner can reply to, and take over, an Instagram conversation from the
dashboard** — the one deliberate reversal of the original "nothing built toward a reply
feature" decision, scoped to Instagram only (owner's call, 2026-08-20): the web widget
(`app/static/index.html`) only ever gets a reply inside its own `POST /chat` response,
with no push channel to deliver a dashboard reply through, so a web write path would
need polling added to that customer-facing file — a separate decision, not taken here.
Sending a reply (`POST /admin/api/conversations/instagram/{id}/reply`) calls
`engagement.service.send_owner_reply()`, which sends on Instagram first and only stores
the message — via `chat.service.post_owner_message()`, which also flips
`conversations.owner_active` — once the send is confirmed (or simulated, in dry run):
the dashboard must never show a message as delivered that Instagram never received,
mirroring the "the send is the source of truth" rule `create_order` and the payment link
already follow. `chat/agent.py` checks `repository.is_takeover_active()` right after
storing the customer's turn and, if set, returns immediately — no tools, no reply, and
none of the shipping/feedback/payment side-effect checks run again until the owner hands
the conversation back (`POST .../resume`). **Owner messages are stored with
`role=ROLE_MODEL`, not a role of their own** — a new role would make `agent.py`'s
Gemini-history builder treat it as a customer turn once the bot resumes, putting two
customer turns in a row and corrupting what the model believes was said. Instead
`chat/repository.py`'s `OWNER_PROVIDER` sentinel rides in the existing `provider` column,
and `admin/conversations/service.py` turns `role` + `provider` into the `author`
(`customer`/`bot`/`owner`) the frontend actually renders on. `ConversationDetail.tsx`
polls its open conversation every 5 seconds while `channel === 'instagram'` so an
incoming reply appears without reopening the panel — safe here specifically because it
is internal-dashboard code, not the customer-facing widget the web-is-out-of-scope call
was about.

**The frontend has no router library.** The only two screens are the login page and the
dashboard shell; channel/tab selection is React state, not a URL, so there is no deep
link and therefore no SPA-fallback route for FastAPI's static mount to need. RTL is
`dir="rtl"` on `<html>` plus Tailwind's logical-property utilities throughout
(`ms-*`/`me-*`, `justify-start`/`justify-end`) — a component styled with physical
`ml-*`/`mr-*` will not flip. Numbers and dates stay LTR inside Arabic text (`.ltr-num`),
and the revenue chart's axis is deliberately locked `dir="ltr"` on purpose — a time axis
running newest-to-oldest right-to-left read as more confusing than briefly breaking page
direction for one component. That one is a judgement call, not a settled fact.

**Nothing about the dashboard's rendering has been verified with a browser.** It was
built and wired without one available: `npm run build` compiles clean, and both the dev
proxy and the production static mount were verified end-to-end over HTTP (login,
`/admin/api/*` calls, asset serving) — but actual layout, the RTL flip, and phone
behaviour are unverified beyond "the code should do this". `dashboard/README.md` has the
specific checklist to run before trusting any of it.

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
intact from the start. Progress is tracked in the README checklist (steps 1–10 done).
Step 11 (verifying channel attribution in the Shopify admin) is still unchecked.

Both Section 9 business questions are now answered by the user: image matching asserts
when confident and asks otherwise; COD orders use `PENDING` with tags `cash-on-delivery`,
`chatbot` and the channel. Keep asking rather than assuming on anything comparable —
these were the user's calls to make, not defaults to pick.

Out of scope by explicit instruction: **no WhatsApp integration.** Instagram *is* now
in scope - Meta Business verification was completed and comments plus DMs are built (see
below). WhatsApp still has no Business Provider. The web widget
(`app/static/index.html`) remains the channel everything is developed against.
