# Wanas Gallery Chatbot

Shopify-connected AI chatbot for Wanas Gallery, built to the plan in
`chatbot-build-from-zero.md`. One FastAPI app, internally a modular monolith
(see Section 3 of the plan and `app/modules/__init__.py` for the boundary rules).

## Running locally

```powershell
.\run.ps1
```

Then open http://127.0.0.1:8000 (test chat widget), http://127.0.0.1:8000/health
(config/integration status) or http://127.0.0.1:8000/docs (API docs).

```powershell
C:\Users\Fathy\anaconda3\envs\wanas\python.exe -m pytest -q
```

## Environment

Python **3.12** in the conda env `wanas`:

```powershell
conda activate wanas
python -m pip install -r requirements.txt
```

> The Anaconda **base** env (Python 3.9.7) cannot be used: its `sqlite3` module
> segfaults on `sqlite3.connect()` because `_sqlite3.pyd` binds to a mismatched
> `sqlite3.dll` found on PATH. Python 3.9 is also past end of life.

Configuration lives in `.env` (gitignored; copy `.env.example` to start).

## The LLM and its fallback chain

Primary model is **`gemini-3.7-flash`** on Google's free tier - the newest stable Flash
model that supports both function calling and native image input, which the catalog
tools and image matching both need.

Free-tier quota is the real constraint, and it is smaller than the published guides
suggest. Measured on 2026-08-17: after roughly 20 requests inside about 40 minutes,
`gemini-3.7-flash` returned

```
429 RESOURCE_EXHAUSTED - Quota exceeded for metric: generate_content_free_tier_requests,
limit: 20, model: gemini-3.7-flash ... Please retry in 30.5s
```

and recovered on its own a few minutes later. Transient `503 UNAVAILABLE` ("high
demand") also happens regularly on the newest model.

`app/integrations/llm.py` therefore walks a chain until something answers, which is this
project's answer to the rate-limit question in Section 9 of the plan:

| Step | What happens |
|---|---|
| 1 | Primary Gemini model, retried with jittered backoff on `503`/timeouts |
| 2 | A `429` is **not** retried on the same model - it is put in cooldown for exactly the delay the API asked for, and later turns skip it immediately |
| 3 | Each `GEMINI_FALLBACK_MODELS` entry in turn - **every Gemini model carries its own separate ~20-request budget**, so the chain spans six of them (3.7 -> 3.6 -> 3.5 -> 2.5 -> 3.5-lite -> 3.1-lite) for roughly 120 requests per window |
| 4 | Only if all six are exhausted: a polite bilingual "please try again in a moment" reply |

Any answer that did not come from the primary is flagged `degraded: true` in the `/chat`
response and shown in the widget, so a silent quality drop is visible while testing.

### Why there is no third-party fallback

A free OpenRouter model (`google/gemma-4-26b-a4b-it:free`) was built as a last-resort
provider and then **removed**. It could call tools, but roughly one catalog question in
five it answered without calling `search_products` at all - once replying "I searched the
catalog and found no t-shirts available" when the store had five in stock. A confident
false statement about stock is worse for a premium brand than a short wait, so the chain
now ends in an honest apology instead. Its free tier was also only 50 requests/day
account-wide, well below Gemini's, and one test request returned HTTP 200 with a body of
keepalive padding and no completion after 120s.

If a second provider is ever wanted, `integrations/llm_types.py` is still
provider-neutral, so it plugs in without the chat module noticing.

### Running on one model

`GEMINI_FALLBACK_MODELS=[]` pins the bot to a single model, which is how the store is
configured now (`gemini-3.1-flash-lite`, chosen by the owner for speed). Two consequences
worth knowing:

- **The daily allowance drops to that one model's ~20 requests.** The chain existed to
  multiply it; without it, request 21 gets the polite apology. A question about a photo
  costs two, because identification is a second call.
- **Image matching is weaker.** Measured on this store's own product photos, with the
  catalog unchanged: `gemini-3.1-flash-lite` identified 1 of 3, `gemini-3.5-flash-lite`
  3 of 3. It fails safe - an unconfident match makes the bot ask instead of assert - but
  the feature is much less useful.

`GEMINI_VISION_MODEL` exists for exactly that: it points image matching at one model while
conversation stays on another. Same photo, same catalog, only that setting changed:

```
vision=(same as chat)         -> RINGER BOXY FIT TSHIRT   confident=False
vision=gemini-3.5-flash-lite  -> RINGER BOXY FIT TSHIRT   confident=True
```

To trade quality for headroom, reorder the chain in `.env`; no code change is needed:

```
GEMINI_MODEL=gemini-3.5-flash
GEMINI_FALLBACK_MODELS=["gemini-3.6-flash","gemini-2.5-flash","gemini-3.5-flash-lite"]
```

## Brand voice

The bot replies in **Modern Standard Arabic** to anyone writing Arabic - including
Egyptian colloquial or Franco-Arabic/Arabizi - and in **English** to English speakers.
No colloquial Egyptian. The full prompt, with its honesty and payment-safety rules, is
`SYSTEM_PROMPT` in `app/modules/chat/agent.py`.

## Build progress

- [x] 1. Scaffolding - `core/config.py`, `core/database.py`, booting `main.py`
- [x] 2. Gemini client + minimal `modules/chat` - text conversation through `POST /chat`
- [x] 3. Shopify client + `modules/catalog` + `search_products` tool
- [x] 4. `modules/orders` read-only lookups
- [x] 5. Image upload through `POST /chat`
- [x] 6. `catalog.service.identify_product_from_image()`
- [ ] 7. `orders.service.create_draft_order()`
- [x] 8. `orders.service.create_cod_order()`
- [x] 9. `modules/support` + `modules/notifications`
- [x] 10. `modules/feedback`
- [ ] 11. Verify channel attribution in Shopify admin

## The catalog

`app/integrations/shopify/client.py` speaks GraphQL (the REST products endpoint has no
keyword search, and REST is Shopify's legacy surface). `modules/catalog` caches the whole
catalog for 5 minutes and ranks matches itself, which buys tolerant matching that
Shopify's query syntax cannot do: partial words (`cairoke` -> Cairokee), Arabic garment
and colour vocabulary (`تيشيرت اسود` -> black t-shirts), diacritics and alef/ya spelling
variants. Ranking weights a title match above product type, above tags, above a size or
colour, above the description.

### Price and stock live on the colour, not the product

This store prices by colour: the RINGER BOXY FIT TSHIRT is 500 EGP in Burgundy and 580
in the other three, and Burgundy is only made in XL. So the tool payload reports an
`available` list of one row per in-stock colour, each with its own price and its own
sizes, rather than a flat list of colours plus a flat list of sizes.

That shape came from two faults in a single live reply: a brown t-shirt quoted as "500
to 580 EGP" when brown is exactly 580, and "Brown, Navy, Beige, Burgundy in M, L and XL",
which reads as twelve combinations when only ten exist. Two flat lists could not have
said it truthfully. When a photo is matched to a colour, `price` is that colour's price.

Guards that matter:

- Draft and archived products are dropped, so they can never be offered.
- Only sizes and colours reported in stock are passed to the model; a fully sold-out
  product is labelled as such instead.
- If Shopify is unreachable and a cached catalog exists, the stale copy is served. With
  no cache the tool reports an error - it never returns an empty list, because "we have
  nothing" would be a lie.
- `online_url` stays `null` while the storefront is password-protected, so the bot cannot
  offer a link that leads to the password page. Publish the products and lift the
  password and links start appearing with no code change.
- `storefront_is_open()` reads that same signal from the cache, and the system prompt
  uses it: while the shop is shut the bot is forbidden to send customers there to order,
  browse or pay. It once answered "you can complete your order directly through our
  online store" - a door the customer cannot open. Unknown counts as closed.

### Two catalog tools, on purpose

`search_products` is keyword-based and cannot see the whole catalog, so it must never be
used for price-ordering questions - asked for the cheapest item it guessed t-shirts at
500 EGP while HEART TOP sits at 200 EGP. `browse_products` filters the cached catalog by
category and price range and sorts by price, so "what is your cheapest item?", "I only
have 300 EGP" and "all hoodies, cheapest first" are answered completely. The system
prompt tells the model which to reach for.

## Sending photos

`POST /chat` takes `images: [{data, mime_type?}]` alongside `message`; either one alone
is a complete turn, since customers routinely send a picture and type nothing. `data` is
base64, bare or as a browser data URL - the widget passes FileReader's output straight
through, and a future channel adapter can hand over downloaded bytes in the same field.
Base64-in-JSON rather than a multipart upload keeps one request shape for every channel
and keeps the endpoint testable with a plain JSON client.

The widget takes photos from the picker, a drag onto the panel, or a paste - useful,
since a customer's "do you sell this?" is usually a screenshot. Attachments show as
thumbnails before sending and in the sent bubble.

Uploads are guarded in `modules/chat/attachments.py`: at most 3 images, 8 MB each and
16 MB per message, rejected on the encoded length before anything is decoded. **The
declared content type is not believed** - the bytes are sniffed, and a PDF or an
executable labelled `image/jpeg` is refused. HEIC is accepted deliberately: it is what an
iPhone camera produces by default, and iPhones also mislabel it, which is the ordinary
case of a declared type being wrong rather than hostile. Rejection messages are written
to be shown to the customer, and the turn is not recorded, so a retry replays nothing.

Image bytes are **never stored**. History keeps `[image]` (or `[2 images]`) where a photo
was, so a captionless turn is not an empty row, and the prompt tells the model it can see
a picture only in the message it arrived with and must ask for it again otherwise.

### Matching a photo to a product

`identify_product_from_image` shows the photo to a model together with a compact list of
the real catalog - titles, types and colours - and asks which of *those* it could be. No
embeddings or vector database: at eighteen products that would be engineering for a
problem this store does not have. Product images are not sent either; the titles and
colours are what the model has to choose between, and eighteen photos would swamp the
request.

The store owner chose **assert when confident, ask otherwise**. That makes `confident`
the load-bearing value, so it is not the model's own word for it. A claim is asserted
only if it survives four checks in `_verify()`:

| Check | The failure it prevents |
|---|---|
| The model itself said "high" | Acting on a guess it never stood behind |
| The named product exists in the catalog | A plausible-sounding piece the store never sold |
| Any colour it claims is a real option on that product | "Your pink one" for a piece never made in pink |
| No second candidate is equally rated | A coin toss between two products dressed up as an answer |

Anything else comes back `confident: false` with the candidates still attached, and the
prompt requires the bot to put them to the customer as possibilities and ask which is
theirs. `count: 0` means nothing matched, which is a real answer - the photo is probably
another brand's piece.

Measured against the live store: three product photos were each identified correctly,
with the right colour. Two unidentifiable images (plain colour swatches) were correctly
refused. Then the same three photos were shown again with the product they actually
depict **removed from the catalog**, where the honest answer is "nothing here matches" -
**0 of 3 produced a false assertion**. Small sample, but that is the failure this design
exists to prevent. The middle path - unsure, candidates offered - is covered by tests but
has not yet been seen against a real photo, because the store's own photos are
distinctive enough to either match or not.

Before this existed, the bot asked "which product is it?" would describe the garment,
search the catalog on its own description, and announce *"That is our Ringer Boxy Fit
T-shirt in brown"* - right only because the picture came from that product's listing.

## Taking a cash-on-delivery order

COD is the one payment path that works while the storefront is password-protected: the
order is created straight through the Admin API, with no checkout page for the customer
to reach. `create_cod_order()` follows the store's existing convention, confirmed by the
owner - `financialStatus: PENDING`, tagged `cash-on-delivery` and `chatbot` so staff can
filter for cash to collect.

The third tag is the **channel**, and it comes from the request rather than from the
model. The orders already in the shop are tagged `whatsapp` because that is where they
came from; letting the model choose would eventually put `whatsapp` on a web order and
misfile it.

Because there is no checkout page, everything a checkout would normally collect and
validate has to happen here:

- Name, phone, street address and city are all required, and the phone must be complete.
- The variant is resolved from the **live catalog** at creation time, not trusted from
  the caller, so an order cannot be placed for a size that does not exist or sold out
  while the customer was deciding. A rejection carries what *is* available, so the bot
  can offer it.
- The price is never sent - only the variant id and a quantity. Nothing in a conversation
  can set the price of a real sale.
- `inventoryBehaviour: DECREMENT_OBEYING_POLICY` takes the stock but refuses to oversell.
- An identical order for the same phone within 15 minutes returns the first order instead
  of creating a second. That is checked against Shopify rather than in memory, so it
  survives a restart. A duplicate parcel costs the store real money, and the likeliest
  cause is the model calling the tool twice in one turn.

The confirmation rule lives in the system prompt, because nothing in code can verify that
a customer agreed: the bot must read the whole order back - items, colours, sizes, price,
name, phone, address - and wait for a reply before calling the tool. The tool's
`customer_confirmed` argument is a speed bump the model fills in itself, not a guarantee.
Observed working: told "please place the order" before any read-back had happened, the
bot read the order back and asked again instead of creating it.

### Four fields Shopify will not fill in for you

An order placed through the bot arrived in the admin reading "Government is missing",
"No customer" and "Shipping is not required". None of that was a Shopify quirk - each was
a field `orderCreate` simply does not infer:

- **`provinceCode`** - Egyptian addresses need the governorate, and free text will not do.
  `governorates.py` maps the 29 governorates (Shopify's own codes, read from its
  `countries.json`) from Arabic or English spellings, so "المنوفية", "Monufia" and
  "Menoufia" all become `MNF`. An unrecognised name is logged and the order still goes
  through without it: a missing field is an annoyance for staff, a refused order is a
  lost sale.
- **`requiresShipping`** on each line - `orderCreate` defaults it to **false**, even
  though the variants are physical goods with `inventoryItem.requiresShipping: true`.
  Left alone, every order claims it needs no delivery.
- **The customer** - without one the order is filed under "No customer". `toUpsert`
  cannot be used: Shopify answers "requires at least one of id, email", and a
  cash-on-delivery customer usually gives only a phone. So the customer is looked up by
  phone, created if new, and attached with `toAssociate`. As with orders, Shopify's
  customer search matches loosely, so a returned row is re-checked before it is reused -
  otherwise the order would be filed under whoever happened to come back first. A failure
  here never costs the order: an order with nobody attached is still an order.
- **`shippingLines`** - the store's own rate, read from Shopify (`read_shipping` scope)
  and cached for an hour, so changing a rate in the admin is enough and nobody has to
  remember a setting too. The cheapest rate that covers the destination province and the
  order value wins; carrier-calculated rates are skipped, since a chat order cannot get a
  live quote. `COD_SHIPPING_FEE` is only a fallback for when the rate cannot be read, and
  with neither available the order simply carries no delivery line - a made-up charge
  would change what the courier collects.

The bot now asks for the governorate as its own question. The order that exposed this had
the governorate typed into the city box, because nothing had asked for it separately.

### Quoting delivery before the customer commits

Asked what delivery costs, the bot answered "ليس لدي معلومات حول تكلفة التوصيل" - the rate
was only ever read inside order creation, long after the customer needed it. With cash on
delivery the courier collects goods *plus* delivery at the door, so `get_delivery_cost`
now exposes the store's live rate, and the prompt requires quoting it before the order is
read back.

It answers even before an address is known, whenever every zone charges the same - which
is how this store is set up, so "how much is delivery?" is answerable immediately. Where
rates genuinely differ by destination it declines rather than picking one.

Districts resolve too: almost nobody writes their governorate, so "المعادي" and "مدينة نصر"
map to Cairo, "الدقي" to Giza, and so on. Only unambiguous districts are listed, and
Helwan and 6th of October are deliberately absent because Shopify treats them as
governorates in their own right.

### The brand voice was formal, then it was not

The bot originally answered every Arabic customer in Modern Standard Arabic, in the tone
of a boutique attendant. The owner changed it (2026-08-18) to everyday Egyptian Arabic and
a friendly voice: a customer who writes "عايز اعرف عندكو ايه" should not be answered in
newsreader Arabic.

Telling the model "be friendly" is not enough for a lite model - it slides back into
الفصحى within a turn or two. The prompt names the forms both ways: use ده، دي، دلوقتي،
عايز، معلش، تمام; avoid هذا، هذه، الآن، سوف، لدي، يمكنك أن. The closing language check
asks it to read its own reply back and rewrite anything that sounds like a bulletin.

The one guard that mattered: **tone changes how it sounds, never what it says.** That
sentence sits in VOICE so a friendlier register cannot loosen the honesty, price and
stock rules underneath it.

### A model answered English in Arabic

An English complaint got a fully Arabic reply, in the very first turn of a fresh
conversation. The LANGUAGE rule is at the top of the system prompt, which has grown past
10,000 characters as each build step added guidance, and it was lost by the time the model
reached the end. The rule is now restated as the last thing in the prompt, where recency
helps, and a test asserts it stays in the final fifth. Fixed on retest.

Worth remembering as the prompt keeps growing: **position matters, and the top of a long
prompt is not a safe place for a rule that must always hold.**

### A model invented a delivery charge

Handed a correct tool result of 118 EGP, `gemini-3.5-flash-lite` told the customer
**50 EGP** - a plausible-sounding Egyptian shipping price it produced from nowhere - and
then quoted 118 correctly one turn later. The path was right; the model was not.

Two changes followed. The tool now hands over finished strings (`cost`, `items_total`,
`total_with_delivery`) so there is nothing to add up or half-remember, and the prompt says
outright that whatever the model believes shipping usually costs "is not evidence and must
never appear in a reply". Both were verified against the same conversation.

This is the failure that removed the OpenRouter fallback in step 2, now on money. The lite
models sit last in the chain and only answer once the better ones are out of quota; if it
recurs, dropping them is the same trade as before - less headroom, fewer confident
falsehoods.

### Phone numbers must be sent in international form

The first real order came back `order.phone: Phone is invalid`. Shopify will not accept
`01000000000`, which is exactly how every Egyptian customer writes their own number - it
wants `+201000000000`. `to_e164()` converts on the way out, while the staff note keeps
the customer's own spelling, since that is what they will recognise when someone reads it
back to them. Left unfixed, this would have blocked every single order.

## Support tickets

The bot cannot refund, replace, re-route a parcel, or cancel an order. When a customer
needs one of those, `create_support_ticket` writes it down and emails the store owner,
and the customer gets a reference like `WG-K7P3QX` to quote later. The reference alphabet
deliberately excludes O/0, I/1, S/5 and B/8, because a customer reads it down a phone.

**The ticket is stored before anyone is notified, and the email is best-effort.** That
ordering is the whole reason `notifications` is a separate module from `support`: a mail
server being down downgrades the outcome from "someone was emailed" to "it is written
down", instead of losing a complaint. Nothing in `notifications/service.py` raises;
failures return False and log loudly. `smtplib` is blocking, so sending happens in a
worker thread - a slow mail server must not hold up a customer's reply.

With no SMTP configured (the state today) tickets are still stored and still get a
reference; the log says at WARNING that nobody was emailed. Set `SMTP_HOST`, `SMTP_FROM`
and `STORE_OWNER_EMAIL` to turn delivery on. `Reply-To` is set to the customer's email
when they gave one, so hitting reply reaches them rather than the robot.

The email is written to be acted on without opening anything else. Alongside the
reference, category, contact and what the customer said, it carries the order as it stands
right now - status, payment, the items with their variants, goods and delivery split
apart, the phone the store actually has on the order, and a one-click link into the
Shopify admin - and the conversation transcript underneath. The transcript is there
because the summary is the *model's* account of a conversation it also conducted; the two
disagreeing is worth seeing.

All of that is gathered after the ticket is stored and every bit of it is optional. If
Shopify is unreachable the email says "could not read #1006, check by hand" rather than
going missing. If the contact the customer gave does not match the one on the order the
email says so instead of hiding the order - somebody quoting an order they cannot prove is
theirs is precisely what a human should be looking at.

One issue is one ticket: the same category from the same conversation inside 30 minutes
returns the existing reference rather than filing again, so a customer restating a
complaint does not fill the owner's inbox. And the prompt keeps a question from becoming
a complaint - the bot must try the catalog and order tools first.

## Order lookup

`get_order_status` needs two things, not one: the order number **and** the email or
phone recorded on the order. Order numbers are sequential (#1001, #1002, ...) and an
order carries a name, a phone number, a delivery city and a purchase history, so a
lookup by number alone hands a customer's details to anyone who can count. Set
`ORDERS_REQUIRE_CONTACT_VERIFICATION=false` to drop the check on a closed test store.

The check is made in `modules/orders/service.py`, **not** by Shopify's search, because
Shopify's search cannot do it. Measured against the live store:

```
'phone:01067177129'  ->  ['#1001', '#1002', '#1003']     # only #1003 has that number
```

Its filters rank by relevance rather than matching exactly, so `orders(query: "phone:...")`
returns other customers' orders too. Every result is re-checked here against the
normalised contact before it is returned - which also means a `name:` filter matching
`#100` against `#1003` cannot slip through.

Phone numbers are compared on their last ten digits, so `+201067177129`, `00201067177129`,
`01067177129`, `0106 717 7129` and `٠١٠٦٧١٧٧١٢٩` are all the same number; anything
shorter than that never matches, so a fragment cannot act as a wildcard. A failed check
and a non-existent order return the identical `not_found`, so the tool cannot be used to
discover which order numbers are real.

What the model receives is deliberately narrower than what Shopify returns: no street
address, no phone, no email, no internal note, no staff tags, no Shopify id.

### Open questions still to settle (Section 9)

- **Rate limits** - answered above by the fallback chain.
- **COD tags / financial status** - business decision, needed at step 8. The store's
  existing orders (created by an earlier bot) already use `financial_status: PENDING`
  with tags `cash-on-delivery`, `chatbot` and `whatsapp` - worth confirming as the
  convention rather than inventing a new one.
- **Image-match confidence bar** - answered: assert when confident, ask otherwise.
  What "confident" means is enforced in code, not left to the model; see above.
