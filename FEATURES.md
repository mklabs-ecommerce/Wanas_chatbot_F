# Wanas Gallery Chatbot — What It Does, What's Next

**Status as of 2026-08-19** — 433 automated tests passing, running locally at
`http://127.0.0.1:8000`, connected to the live Shopify store `p0hd05-m5`.

This document is written for deciding what to build next. It covers what the bot can
genuinely do today, what is deliberately not built yet, and what I would recommend adding.

---

## 1. What the bot can do right now

Everything in this section is built, tested and working against the real store.

### 1.1 It talks like someone in the shop

- **Everyday Egyptian Arabic** (العامية المصرية) for anyone writing Arabic — including
  formal Arabic and Franco/Arabizi written in Latin letters.
- **Friendly, natural English** for anyone writing English.
- It follows the customer if they switch language mid-conversation.
- Product names, sizes and colours are always kept exactly as the store records them,
  even when the rest of the reply is translated.
- Short replies — two or three sentences for a simple question, never a wall of text.
- No emoji, no markdown, plain text only.

Tone changes how it sounds, never what it says. The honesty rules below outrank it.

### 1.2 Product search and browsing

Two separate abilities, because they answer different kinds of question:

| Customer asks | Tool used | Why |
|---|---|---|
| "do you have the Cairokee tee?" | `search_products` | Keyword match on specific items |
| "what's the cheapest hoodie?" / "anything under 500?" | `browse_products` | Sees the **whole** catalog, so price answers are complete |

- Matching is tolerant: partial words (`cairoke`), Arabic garment and colour vocabulary,
  diacritics, and alef/ya spelling variants all work.
- Browsing can filter by category (T-Shirts, Polo Shirts, Hoodies & Sweatshirts,
  Joggers & Sweatpants, Jackets, Tops), by minimum and maximum price, and sort cheapest
  or most expensive first.
- The catalog is cached for 5 minutes, so repeated questions do not hammer Shopify.

**Price and available sizes belong to a colour, not to a product.** This store sells the
same t-shirt at 500 EGP in Burgundy (XL only) and 580 in Brown/Navy/Beige. The bot is
given one row per in-stock colour with its own price and its own sizes, so it can never
offer a size and colour combination that does not exist.

### 1.3 Identifying a product from a photo

A customer can send up to **3 photos** (JPEG, PNG, WebP, HEIC/HEIF, GIF — up to 8 MB
each, 16 MB total). The file type is detected from the actual bytes, not from what the
browser claims, because phones routinely mislabel HEIC as JPEG.

The photo is shown to the model alongside a compact index of the real catalog, and the
model is asked which of *those* it could be. It cannot invent a product.

**It asserts only when it is genuinely confident.** "Confident" is decided in code, not
by the model, and requires all of:

1. the model itself reported high confidence,
2. the product it named actually exists,
3. the colour it claimed is a real option on that product,
4. there is no equally-rated runner-up.

Fail any one, and the bot shows the candidates and *asks* instead of guessing.

Photos are never stored. History records `[image]`, and the photo lives only for the turn
it arrived on.

### 1.4 Checking an order

- Look up by **order number**, or list a customer's recent orders by **email or phone**.
- Returns status, payment state, items with sizes and colours, totals, delivery city and
  tracking.
- Phone numbers match on their last ten digits, so `+20`, `0020`, `0` and Arabic-Indic
  numerals all work.

**An order number alone never opens an order.** Order numbers are sequential and
guessable, and the record holds a name, phone, city and purchase history — so the
customer must also give the email or phone that is on the order. A wrong contact and a
non-existent order return the *same* answer, so the bot cannot be used to discover which
order numbers are real.

No street address, no internal note, no staff tag and no Shopify ID ever reaches the model.

### 1.5 Quoting delivery

Read live from the store's own shipping rates, by governorate. The bot is required to
check this *before* reading an order back, because with cash on delivery the customer
hands the courier the goods plus delivery, so they must be told the full figure before
agreeing. If the rate cannot be read, it says so rather than naming a number.

**How long it takes** is stated too — right after confirming an order, and any time a
customer asks. Currently **3 to 5 working days**, the same for every governorate.

That figure lives in `.env` (`DELIVERY_DAYS_MIN` / `DELIVERY_DAYS_MAX`), not in Shopify —
checked on 2026-08-19, Shopify carries no delivery time at all: the store has one Domestic
zone covering all 29 governorates with a single rate whose description is empty. Change
those two values and the bot changes what it says.

If they are ever left unset the bot says it cannot say exactly and the team will confirm.
It never names a number of days that did not come from a tool, and it never turns the
range into a date — no "it will arrive Tuesday".

### 1.6 Placing a cash-on-delivery order

The bot can create a **real order in Shopify**. It collects name, phone, street address,
city and governorate, reads the whole thing back, and only creates the order once the
customer confirms.

Convention: status `PENDING`, tagged `cash-on-delivery`, `chatbot`, and the channel.

Guards that make this safe:

- Sizes and colours are **re-checked against live stock** at the moment of creation — the
  conversation's claim is not trusted, so a sold-out size cannot be ordered.
- Only the variant and quantity are sent to Shopify — **never a price**. The store's own
  pricing always wins.
- Stock is decremented **obeying policy**, so an order fails rather than overselling.
- An identical order for the same phone within 15 minutes returns the existing order
  instead of creating a second one. This is checked against Shopify, so it survives a
  restart.
- Order creation is the one Shopify call that is **never retried** — an ambiguous failure
  must not quietly become two orders.
- Egyptian governorate is matched from Arabic or English across all 29, and phone numbers
  are converted to international format, both of which Shopify silently requires.

### 1.7 Cancelling an order, and exchanges

Straight from `policy.md`.

**Cancelling** — the bot does it itself, in Shopify, and emails you every time.

- Only **before the order ships**. Once it has shipped it says so and explains the
  exchange route instead.
- Only if the customer proves the order is theirs — same check as an order lookup.
- Never on an order with money on it. An unshipped cash-on-delivery order is money that
  never moved; anything paid is a refund decision for you, not the bot.
- The pieces are **restocked** automatically, so they go back on sale.
- Shopify cancels in the background, so if it has not finished the bot says "it is being
  cancelled" rather than claiming it is done.

**Exchanges are explained, not performed** — your policy makes them a courier-and-human
process, so the bot states the terms and files a ticket for you:

- return at the door through the courier, paying shipping only;
- otherwise an exchange within **24 hours**, in original packaging, unworn and clean;
- faulty or wrong item → the store pays; changed their mind → the customer pays, plus
  **20 EGP** for the exchange delivery.

It is forbidden to waive a fee, soften the terms, or promise an exchange will be accepted.

### 1.8 Telling a customer their order shipped

When Shopify marks an order fulfilled, the customer is told once — with the tracking
number if there is one.

- It says only that the parcel **left the shop**, which is all Shopify knows. Never where
  it is now, what stage it is at, or when it will land.
- Cancelled orders are never announced, and neither are orders that have **already
  arrived** — telling someone their parcel is on its way while they hold it is worse than
  saying nothing.
- Said once. It is not repeated on later messages.

**It rides on the customer's next message.** A web chat widget cannot start a
conversation, so the bot can only tell them when they next write in. The detection half
is done and tested; when WhatsApp is connected, the same logic sends it directly.

### 1.9 Support tickets

When the bot cannot resolve something, it files a ticket and emails the store owner.

Categories: damaged or faulty item, wrong item received, delivery problem, return or
exchange, change or cancel an order, payment problem, complaint, other.

The customer gets a reference like `WG-K7P3QX` (deliberately avoiding O/0, I/1, S/5, B/8,
because people read these aloud).

**The ticket is stored first and the email is best-effort**, so a dead mail server can
never lose a complaint. If email is not configured the ticket is still filed and the
customer still gets a reference; a warning notes that nobody was emailed.

The email carries far more than the ticket itself:

- the **order as it stands right now** — status, payment, items with variants, goods and
  delivery split apart, the contact and city actually on the order, tracking, and a
  one-click link into the Shopify admin;
- the **conversation transcript**, because the summary is the *model's* own account of a
  chat it also conducted;
- a warning if the contact given in chat does not match the one on the order.

The same issue in the same conversation within 30 minutes returns the existing ticket
rather than filing again.

### 1.10 Recording what customers think

Separate from support tickets on purpose: a ticket is a problem awaiting an action,
feedback is an opinion awaiting nothing. Anything the customer wants fixed, replaced,
refunded or chased stays a ticket.

- **Customers are never asked to score anything** — no stars, no 1 to 5. They say what
  they think in their own words, and it is stored exactly as they wrote it, untranslated.
- **It asks only once the order has actually arrived**, and only about the pieces — the
  fit, the fabric, the colour, whether it matched the photos. It never asks at order
  time: a customer who has not received the garment has no opinion of it to give.
- "Arrived" is read from Shopify, not guessed. For cash on delivery the customer pays the
  courier at the door, so **`PAID` means it was handed over**. Being marked shipped is not
  enough. The bot re-checks on each turn of the conversation, so the moment you mark the
  order paid, the next thing the customer says triggers the question.
- The question names the actual pieces, because "how was your order?" gets nothing back.
- If they would rather not answer, it lets it go and never asks again.
- **Only unhappy feedback emails you.** Everything is stored; praise does not fill your
  inbox. That email carries the same context a ticket does — the live order, the
  transcript, and a link into the Shopify admin.
- If the sentiment cannot be read, it is treated as **negative**, so it reaches you. A
  complaint filed as "fine" is a complaint lost; an unnecessary email costs nothing.
- No reference number is given out. Unlike a ticket, nobody chases feedback up, and a
  code would invite the customer to expect a reply that is not coming.
- One conversation is one opinion, so saying thank you twice is recorded once.

### 1.11 Honesty guarantees

These are the rules that stop the bot inventing things, and each one exists because of a
real bug that happened:

- It cannot state stock or prices it has not read from Shopify.
- Products that are not `ACTIVE` are dropped; only in-stock sizes and colours are shown.
- If Shopify fails, the last good catalog is served. If there is no cache at all it raises
  an error rather than returning an empty list — "we have nothing" would be a lie.
- It never invents a delivery charge.
- **While the storefront is password-protected it offers no links and never tells anyone
  to order or pay on the website** — that would send them to a page they cannot get into.
  Publishing the store fixes this with no code change.
- Tool results carry data only, never instructions — a result once contained
  "Tell the customer…" and the bot read it aloud to them.

---

## 2. Settings you can change without touching code

All in `.env`:

| Setting | Now | What it controls |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | The model that answers |
| `GEMINI_VISION_MODEL` | `gemini-3.5-flash-lite` | The model that reads photos |
| `GEMINI_FALLBACK_MODELS` | `[]` (none) | Models to try when the first is out of quota |
| `COD_SHIPPING_FEE` | unset | A flat delivery charge, if you want one |
| `COD_ORDER_TAGS` | `cash-on-delivery`, `chatbot` | How bot orders are tagged in Shopify |
| `COD_DUPLICATE_WINDOW_SECONDS` | 900 (15 min) | Duplicate-order protection window |
| `TICKET_DUPLICATE_WINDOW_SECONDS` | 1800 (30 min) | Duplicate-ticket window |
| `FEEDBACK_DUPLICATE_WINDOW_SECONDS` | 1800 (30 min) | Duplicate-feedback window |
| `ORDERS_REQUIRE_CONTACT_VERIFICATION` | `true` | The order-privacy check in §1.4 |
| `CATALOG_CACHE_SECONDS` | 300 | How long the catalog is cached |
| `CHAT_HISTORY_LIMIT` | 20 | Messages of context the bot remembers |
| `DELIVERY_DAYS_MIN` / `MAX` | 3 / 5 | The delivery window the bot quotes |
| `DASHBOARD_TOKEN` | set | Turns the owner dashboard on; 16+ chars |
| `STORE_OWNER_EMAIL` | `mklabsecommerce@gmail.com` | Who gets support tickets |

---

## 3. What is not built yet

### 3.1 Agreed and planned

| # | Feature | Status |
|---|---|---|
| 7 | **Online card payment** — create a Shopify draft order and send the customer a real checkout link | **Deferred** until the storefront password is lifted. A checkout link is useless while the store is shut. |
| 10 | **Feedback module** — record what customers think | **Built** (2026-08-19). See §1.8. |
| 11 | **Verify channel attribution** in the Shopify admin | Orders #1004 and #1006 are sitting there to check. |

### 3.2 Things the bot tells customers it cannot do

It says these plainly rather than pretending:

- It cannot **change what is in** an order once placed — only cancel it, before it ships.
- It cannot take **card or online payment** yet.

### 3.3 Excluded by decision

**No WhatsApp, Instagram or Facebook integration.** No Meta Business Provider exists.
Everything is built and tested against the local web chat widget. The core logic assumes
nothing about the channel, so these would be thin adapters later — but they are out of
scope for now.

---

## 4. Limits worth knowing before going live

- **LLM quota is the hard ceiling.** The store runs a *single* model at your request, with
  no fallback. That is roughly **20 requests per rolling window**. When it is gone, every
  customer gets a bilingual apology until it resets. There is no degraded-but-working path.
- **No third-party fallback, deliberately.** An OpenRouter free model was built and then
  removed: it answered catalog questions without calling the search tool and stated false
  stock levels roughly once in five. Anything added here must not repeat that.
- **The storefront is password-protected**, so the bot offers no product links.
- **The `/chat` endpoint has no authentication and no rate limiting.** Fine for local
  testing; see §5 before exposing it publicly.

---

## 5. Recommendations

Ordered by what I would do first. Each notes roughly what it costs.

### 5.1 Before any public launch

**a) Put the LLM chain back, or move to a paid tier.** *(config change, or small cost)*
This is the single biggest risk to a live launch. One model with no fallback and ~20
requests per window means a handful of customers can take the bot offline entirely. The
fallback chain already exists in code and spans six models — re-enabling it is one line
in `.env`. Moving to a paid tier is a config change, not a rebuild.

**b) Add authentication and rate limiting to `/chat`.** *(small)*
Right now anyone who can reach the endpoint can place real cash-on-delivery orders and
burn the daily quota. A shared key plus a per-conversation and per-IP limit would close
both. This matters more than usual precisely because quota is so tight.

**c) Publish the storefront.** *(no code at all)*
This unlocks three things at once: the bot can share product links, it can stop saying
customers cannot order on the website, and step 7 (card payment) becomes worth building.
The code already detects this and adjusts itself — nothing to change.

### 5.2 High value, moderate effort

**d) Let customers change or cancel an order.** *(medium)*
There is already a whole ticket category for it, which tells you customers ask. Today
every one of those becomes an email for a human. Within a safe window — before it ships,
same day — the bot could cancel and restock in Shopify directly. This is probably the
most-requested thing it currently cannot do.

**e) ~~Build the feedback module (step 10).~~** **Done 2026-08-19** — see §1.8.
Worth pairing with (f) below: feedback is being collected now, but there is still no way
to read it back other than the emails for unhappy customers.

**f) Give the owner a way to read what happened.** *(medium)*
Every conversation is already stored, but there is no way to look at them. A simple
owner-only page — conversations, which tools ran, where the bot gave up, which questions
it answered badly — would turn the bot from a black box into something you can improve
deliberately. Cheap, because the data is already there.

### 5.3 Worth considering later

**g) Follow up on abandoned orders.** The bot often collects a name, phone and address
and then the customer disappears. Those are warm leads sitting in the conversation
history, and nothing currently does anything with them.

**h) Back-in-stock notifications.** Today "that size is sold out" ends the conversation.
Capturing the request and telling the customer when it returns turns a dead end into a
sale.

**i) A weekly digest email to the owner.** Orders placed, tickets filed, most-asked
products, questions the bot could not answer. Reuses the notifications module that
already exists.

**j) Arabic voice notes.** Egyptian customers overwhelmingly prefer voice over typing.
This only becomes relevant alongside a messaging channel, which is out of scope today —
but it is worth knowing it is the natural next step if that ever changes.

---

## 6. Where things live

```
POST /chat  →  modules/chat/router.py  →  agent.handle_message()
                                           ├─ conversation history
                                           ├─ integrations/llm.py → Gemini
                                           └─ tools.dispatch() → <module>/service.py
```

Modules: `catalog`, `orders`, `support`, `notifications`, `chat`, `feedback`.
Each is reached only through its public `service.py`, and each owns its own tables.

- `/` — the test chat widget
- `/health` — which integrations are configured
- `/docs` — API documentation
