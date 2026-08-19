# Model selection brief — Wanas Gallery chatbot

**Purpose of this document:** hand it to another assistant and ask it to recommend the
best model for the lowest price. Everything needed to answer is below; no other context
about the project is required.

**The question to answer:** which model (and which fallback chain, if any) should this
chatbot run on, given that it must do every job in section 2 reliably, and cost as little
as possible?

Prepared 2026-08-18. Figures marked **measured** were taken from this running system
against the live store; figures marked *estimated* are not.

---

## 1. What the product is

A customer-facing chat assistant for **Wanas Gallery**, a small premium clothing brand in
Egypt, connected to their live Shopify store (18 active products, prices in EGP).
Customers talk to it in a web chat widget. It answers questions about the catalog, looks
up orders, identifies garments from photos, and places real cash-on-delivery orders.

It is a **real commercial system, not a demo**: it creates orders that staff pack and
couriers deliver, and it quotes the cash amount a courier will collect at the door.

---

## 2. What the model must be able to do

All six capabilities are required. A model that cannot do any one of them is not a
candidate.

### 2.1 Tool calling — required, heavily used

Seven tools are declared on **every** request, as plain JSON Schema:

| Tool | What it does |
|---|---|
| `search_products` | Keyword search of the live catalog |
| `browse_products` | Whole-catalog listing by category/price |
| `get_order_status` | One order, gated on the customer proving ownership |
| `get_orders_by_customer` | A customer's recent orders |
| `get_delivery_cost` | The store's live delivery rate |
| `identify_product_from_image` | Matches an attached photo to catalog products |
| `create_cod_order` | **Creates a real order in Shopify** |

The loop is driven by hand, not by any SDK's automatic function calling: the model is
asked, its tool calls are executed, results are fed back, up to **4 rounds** per customer
turn. The model must therefore handle multi-round tool conversations and tolerate having
tool results replayed to it.

### 2.2 Native image input — required

Customers attach photos ("do you sell this?"). Up to **3 images per message**, 8 MB each,
in **JPEG, PNG, WebP, HEIC, HEIF or GIF**. HEIC matters specifically: it is what an
iPhone camera produces by default, and a large share of customers are on iPhones.

Images are passed as inline bytes, not URLs.

### 2.3 JSON-only output mode — required

The image identifier makes a **separate** model call that must return strict JSON and
**no tools are offered on that call**. If the candidate model cannot be constrained to
JSON output, or can only do so while tools are also declared, that is a problem.

### 2.4 Arabic, at a specific register — required

- Customers write **Modern Standard Arabic, Egyptian colloquial, or Arabizi** (Arabic
  written in Latin letters, e.g. `3ayez t-shirt aswad`).
- The bot must **always reply in Modern Standard Arabic** to any of those, and in English
  to English. Egyptian colloquial output is not acceptable for this brand.
- It must handle Arabic-Indic digits in input (`٠١٠٦٧١٧٧١٢٩`).
- Product names, sizes and colours stay in their original language mid-sentence, so the
  model must code-switch cleanly inside one reply.

Arabic quality is a first-class requirement, not a nice-to-have. Roughly half of real
traffic is Arabic.

### 2.5 Refusing to invent facts — required, and the hardest one

This is where models have actually failed us (see section 5). The model must never state
a price, stock level, size, colour, delivery charge or order status that did not come
back from a tool. It routinely knows plausible-sounding answers — "shipping in Egypt is
about 50 EGP" — and saying one is worse than saying nothing, because a customer is being
asked to hand over cash.

### 2.6 Latency — a real constraint

It is a live chat window. **Under ~5 seconds per turn is good; over ~20 seconds is
unacceptable.** Measured on the current model, turns land at 2–6s including a tool call.

### 2.7 Context length — NOT a constraint

Worth stating plainly so nobody pays for capability we cannot use: the largest realistic
request is roughly **16k tokens** (system prompt + tool schemas + short history + up to
three images). Million-token context windows are irrelevant here. Do not weight them.

---

## 3. Measured token footprint

**Measured** on the running system:

```
system prompt            ~2,542 tokens   identical on every request
tool declarations        ~1,891 tokens   identical on every request
history + user message     ~400 tokens
                         ─────────────
plain turn               ~4,833 tokens input
turn with a tool call   ~10,266 tokens input  (two model calls)
output per turn            ~150 tokens  estimated
```

Extra calls:
- An **image question costs two model calls**: the conversation turn plus a separate
  identification call carrying the photo (~260 tokens) and a compact catalog index
  (~277 tokens).

**The single most important cost fact:** ~**4,433 tokens of every request are
byte-identical** (system prompt + tool schemas), and they are sent first, unchanged.
Any model or provider offering prompt/context caching will cut the bill dramatically —
on Gemini this is ~90% off those tokens and applies automatically on 2.5-and-newer
models. **Weight cached-input pricing heavily in the recommendation.**

Token counts use a ~4-characters-per-token approximation. Arabic tokenizes less
efficiently than English, so real usage may run somewhat higher, though the fixed English
prefix dominates.

---

## 4. Volume

**Not yet measured — the store is still in testing.** Please give costs at these three
volumes rather than assuming one:

| Scenario | Conversations/day | Turns/month (≈4 turns each) |
|---|---|---|
| Quiet | 10 | 1,200 |
| Realistic | 50 | 6,000 |
| Busy | 200 | 24,000 |

Assume ~60% of turns involve at least one tool call, and ~10% of conversations include a
photo.

---

## 5. Measured model behaviour — the evidence that matters

These are observations from this system, not benchmarks. They are the reason quality is
weighted above price.

### 5.1 Image identification accuracy

Same three product photos from the store's own listings, same catalog, only the model
changed. "Identified" means it named the correct product *and* passed our verification
checks:

| Model | Result |
|---|---|
| `gemini-3.5-flash-lite` | **3 of 3** |
| `gemini-3.1-flash-lite` | **1 of 3** |

Adversarial check, `gemini-3.5-flash-lite`: the same three photos shown with the correct
product **removed from the catalog**, where the honest answer is "nothing matches" —
**0 false identifications out of 3**. Encouraging but a small sample.

### 5.2 A model invented a price

`gemini-3.5-flash-lite` was handed a tool result of **118 EGP** delivery and told the
customer **50 EGP** — a plausible Egyptian shipping figure produced from nowhere. It then
quoted 118 correctly on the next turn. Mitigated by handing the model finished strings
instead of parts to compute, plus an explicit prompt rule, and not reproduced since.

### 5.3 A model claimed to have used a tool it never called

A free third-party model (`google/gemma-4-26b-a4b-it` via OpenRouter) answered catalog
questions **without calling the search tool** roughly **1 in 5 times**, once replying "I
searched the catalog and found no t-shirts available" when five were in stock. It was
removed from the system entirely. **Any candidate that does this is disqualified**, at
any price.

### 5.4 What all tested Gemini models handled fine

`gemini-3.7-flash`, `3.6-flash`, `3.5-flash`, `2.5-flash`, `3.5-flash-lite` and
`3.1-flash-lite` all support tool calling and image input, and all produce acceptable
Modern Standard Arabic.

---

## 6. Current setup, and what is wrong with it

- **Provider:** Google Gemini, `google-genai` SDK, free tier.
- **Current model:** `gemini-3.1-flash-lite` as the *only* model (no fallbacks), chosen
  by the store owner for speed. Turns land at 2–6s.
- **Sampling:** temperature 0.4 for chat, 0.0 for image identification; Gemini
  `thinking_level: low`.

Two problems with it:

1. **Free-tier quota.** **Measured**: ~20 requests per day *per model*
   (`quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier`, `quotaValue: 20`), not
   the ~1,500/day that public guides claim. With one model and no fallbacks that is ~20
   requests a day total — and a photo question costs two. This is the binding constraint
   today.
2. **Quality.** `3.1-flash-lite` scored 1 of 3 on image identification (section 5.1).

---

## 7. Switching provider is allowed

The codebase is deliberately provider-neutral at the boundary: `app/integrations/
llm_types.py` defines neutral `Turn` / `ToolCall` / `ToolResult` / `ImagePart` shapes, and
the conversation code never imports a vendor SDK. **Moving to a different provider means
writing one new client class in `app/integrations/`, not rewriting the bot.**

So non-Google models are legitimate candidates — but only if they meet **every**
requirement in section 2, in particular:

- multi-round tool calling with replayed tool results,
- inline image input including **HEIC**,
- a JSON-only response mode that works **without** tools declared,
- Modern Standard Arabic of genuinely good quality.

One implementation note if the recommendation stays on Gemini 3.x: function-call parts
carry a `thought_signature` that must be replayed on the following request, or the API
rejects the turn. Already handled; mentioned only so it is not treated as a blocker.

---

## 8. How to judge the candidates

In priority order:

1. **Does not fabricate.** Ranked first because it is the failure that has actually hurt
   us, and because this bot quotes money and creates real orders.
2. **Reliable tool calling** across up to 4 rounds, with 7 tools declared.
3. **Image identification accuracy** against a small catalog of similar garments.
4. **Modern Standard Arabic quality**, from colloquial and Arabizi input.
5. **Latency** — under ~5s per turn.
6. **Price**, given all of the above.

Price is last on purpose. At realistic volume the entire spread between the cheapest
usable model and the best available one has been of the order of a few dollars a month —
far less than the cost of one wrong order. **Do not recommend a weaker model to save a
dollar; do flag anything paying for capability this workload cannot use** (long context,
reasoning depth, audio).

---

## 9. What to give back

1. **A primary model**, with per-1M input/output *and cached-input* prices, and why it
   beats the alternatives on the criteria in section 8 — not just on price.
2. **A fallback chain**, or an explicit recommendation of none, with the reasoning.
3. **A separate vision model if worth it.** The system supports pointing image
   identification at a different model from the conversation
   (`GEMINI_VISION_MODEL`), because recognising a garment and talking to a customer are
   different jobs.
4. **Estimated monthly cost at all three volumes** in section 4, with and without prompt
   caching, stating your assumptions.
5. **Free tier vs paid**: whether the free tier can realistically serve this at each
   volume, given the measured ~20/day/model limit.
6. **Anything time-limited** — models being retired, promotional pricing that expires,
   preview models that should not carry production traffic.
7. **Your confidence**, and what you could not verify.

---

## 10. Candidates already looked at

Checked against section 2 so they are not re-proposed without new information. Capability
data read from OpenRouter's public models API on 2026-08-18.

| Candidate | Outcome |
|---|---|
| `nvidia/nemotron-3-ultra-550b-a55b:free` | **Rejected.** `input_modalities: ["text"]` - no image input at all, so 2.2 fails outright. The `:free` variant also lacks `response_format`/`structured_outputs`, so 2.3 fails too. Tool calling is supported and the context is 1M (irrelevant here). |
| `nvidia/nemotron-3-ultra-550b-a55b` (paid) | **Possible for conversation only, still no vision.** $0.60/$3.60 per 1M - cheaper than `gemini-3.7-flash` at $0.75/$3.75 - and it does have structured outputs. Would need a second model for photos, and a new provider client. Untested for fabrication, which is criterion 1. |
| `google/gemma-4-26b-a4b-it:free` | **Rejected and already removed from production** - see 5.3. |

Free-tier limits on OpenRouter, for any `:free` candidate: **20 requests/minute and 50
per day**, rising to 1,000/day once the account has purchased at least 10 credits.
Limits are global per account, so extra keys do not help. Compare with the measured
Gemini free tier of ~20/day *per model*.

If a recommendation involves leaving Google, say plainly what has to be built (a new
client in `app/integrations/`) and whether a second provider is needed for images.

---

## 11. Non-negotiables

- Prices in the answer must be current as of the date you are asked, and sourced. Model
  pricing changes often; do not answer from memory.
- Do not recommend a **preview** or **experimental** model as the primary. This system
  creates real orders.
- Do not recommend a model being retired within 6 months without saying so clearly.
- If two models are close, say so rather than inventing a winner.
