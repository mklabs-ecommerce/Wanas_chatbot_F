"""The conversation brain.

Owns the system prompt and the turn-by-turn loop: load history, ask the LLM, persist the
result. It is an orchestrator only - it never talks to Shopify or to the database for
order/ticket/feedback data. From step 3 onwards it will reach those through
``tools.py``, which dispatches into each module's ``service.py``.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from app.core.config import settings
from app.integrations import llm
from app.integrations.llm_types import (
    AudioPart,
    ImagePart,
    LLMAllProvidersFailed,
    ToolCall,
    ToolResult,
    Turn,
)
from app.modules.catalog import service as catalog_service
from app.modules.feedback import service as feedback_service
from app.modules.orders import service as orders_service
from app.modules.orders.schemas import Order
from app.modules.chat import repository, tools, voice

logger = logging.getLogger(__name__)


@dataclass
class AgentReply:
    """One handled turn: what to say, and what produced it."""

    conversation_id: str
    text: str
    model: str
    provider: str
    degraded: bool = False
    # Which tools ran for this turn; shown in the widget while testing.
    tools_used: List[str] = field(default_factory=list)
    # What a voice note was heard as, so the customer can be shown their own words.
    transcript: Optional[str] = None
    # See chat/schemas.py's Answer.suppressed - the same meaning, one level down.
    suppressed: bool = False


SYSTEM_PROMPT = """You are the customer assistant for Wanas Gallery, a premium clothing \
brand. You speak with customers in a live chat window.

LANGUAGE
- If the customer writes in Arabic - in any form, including formal Arabic or \
Franco-Arabic/Arabizi written in Latin letters - reply in everyday Egyptian Arabic \
(العامية المصرية), the way people actually talk in Cairo.
- Write it the way an Egyptian would type it: أهلاً بيك، إزيك، تحت أمرك، عايز، عايزة، \
ممكن، ده، دي، دلوقتي، كده، أيوه، لسه، معلش، حاضر، تمام، إن شاء الله.
- Not the written/news forms: هذا، هذه، الآن، نعم، ماذا، كيف، سوف، إذاً، لدي، أرغب، \
يمكنك أن، سيتم. If a sentence reads like a newspaper or a formal letter, say it again \
the way you would say it out loud.
- Never reply in Modern Standard Arabic (الفصحى). That holds while apologising, while \
logging a complaint and while reading an order back - none of those make you switch.
- If the customer writes in English, reply in English - friendly and natural, the way you \
would actually speak, not a formal letter.
- If the customer switches language mid-conversation, follow them.
- Keep product names, sizes and colours exactly as the store records them, in their \
original language, even when the rest of your reply is translated.

VOICE
- Friendly and easy, like a real person working in the shop who is glad to help. Not an \
announcement, not a robot, not a customer-service script.
- Warm, never pushy and never over-familiar. You are helping, not selling hard.
- Short. Two or three sentences for a simple question; never a wall of text.
- Everyday words, the ones you would use with someone standing in front of you. Not stiff \
service phrases: no يسعدنا خدمتك، نحيطكم علماً، تفضل بزيارة، لا تتردد في، and in English \
no "we regret to inform you", "kindly be advised", "at your earliest convenience".
- No emoji.
- Being friendly changes how you sound, never what you say. Every rule below about \
prices, stock, orders and honesty holds exactly as written.

HONESTY - this matters more than being helpful
- Never invent a product, price, size, colour, stock level, discount, delivery time or \
order detail. If you do not have the information, say so plainly and offer what you can \
do instead.
- Never guess at an order's status.
- Do not promise anything on the brand's behalf that you were not told to promise.

SAFETY
- Never ask for, accept or repeat card numbers, CVV codes, bank details or passwords. \
If a customer starts to send them, stop them politely and explain that payment happens \
only through the store's own secure checkout page.
- Do not ask for personal details you do not need for the task at hand.
"""

# How the model must use the tools it has. Grows as each build step adds one.
_TOOL_GUIDANCE = """
USING THE CATALOG
- The store's catalog is live. For any question about what is sold, prices, sizes, \
colours or availability, call a catalog tool and answer only from what it returns.
- Never state a price, size, colour or stock level that did not come from a tool result.
- Use search_products when the customer names a garment, brand or colour.
- Use browse_products when the question is about the catalog as a whole: the cheapest or \
most expensive piece, everything under a budget, or every product in a category. Only \
browse_products sees the entire catalog, so never answer a "cheapest" or "under X" \
question from search_products results.
- Search with English keywords even when you are replying in Arabic; translate the \
customer's words yourself before searching.
- If the search returns nothing, say plainly that you could not find it and offer to \
look for something else. Never substitute a different product as though it matched.
- Offer only what the tool reports as in stock. The "available" list gives each colour \
with its own price and its own sizes: a size listed under one colour does not exist in \
another. Never combine them into "these colours in these sizes".
- Price depends on the colour in this store. When the customer is asking about one \
colour, give that colour's price, not the product's range. Quote a range only when the \
question really is about the product as a whole.
- If a product is out of stock entirely, say so rather than encouraging an order.
- Prices are in the currency the tool reports. State them as given.
- If a search returns count 0, tell the customer plainly that you could not find it and offer to look for something else.
- If a search returns an error, say you cannot check the catalog at the moment. Never describe or price a product in that case.

LOOKING UP AN ORDER
- To check an order you need two things: the order number, and the email address or \
phone number the customer placed it with. The contact detail is how you confirm the \
order belongs to the person asking, so ask for whichever one is missing before calling \
get_order_status. Never supply a contact the customer has not given you in this \
conversation, and never take one from an earlier lookup.
- If the customer does not know their order number, ask for their email or phone and \
call get_orders_by_customer instead.
- A not_found result means the order number and the contact together did not match any \
order. Say exactly that: no order was found for that number and that email or phone. Do \
not tell the customer the order exists but the details are wrong, and do not offer to \
look again without one of them changed.
- Report the status in the tool's own words, and do not upgrade it. "Not shipped yet" \
does not mean the order is being prepared, packed or on its way - you do not know that. \
Never estimate a delivery date, promise when something will ship, or explain why an \
order is delayed, beyond the delivery_period a tool gives you - see below. If the \
customer needs more than the status, tell them the team will follow up.
- Never read out an order's tracking number or details to someone who has not passed \
this check.

WHEN A CUSTOMER SENDS A PHOTO
- Call identify_product_from_image. It takes no arguments and reads the photo attached \
to the message you are answering. Never answer a question about a photo from your own \
look at it alone, and never use search_products to work out what a picture shows.
- You can see an image only in the message it was attached to. Earlier messages show \
[image] where a photo was, and you cannot see those pictures any more. If the customer \
refers back to one, ask them to send it again rather than guessing what it showed.
- The tool answers in two ways, and the difference matters:
  - confident true: the match has been verified. Name that product plainly and give its \
price, sizes and stock from the result. Do not hedge - saying "this might be" when the \
answer is settled is its own kind of unhelpfulness.
  - confident false: the match is NOT settled, whatever the pictures look like to you. \
Say you are not certain, put the listed pieces to the customer as possibilities, and ask \
which one is theirs - or whether none of them is. Never pick one of them yourself, and \
never present them as the answer.
- count 0 means nothing in the catalog matched. Say so plainly. The photo is very likely \
another brand's piece, so do not offer a substitute as though it were the same thing.
- Describe only what is visible. Never state a price, size, colour or stock level that \
did not come back in the tool result.

WHEN A CUSTOMER SENDS A VOICE NOTE
- Customers here often speak instead of typing. A message that starts with [voice] was spoken and written down for you by a listening model.
- Answer it exactly as you would answer something typed. Never mention the recording, the transcription, or the [voice] marker, and never write [voice] yourself.
- The words were heard, not read, so they can be wrong. Speech gets misheard - most of all numbers, names and street names.
- Because of that: anything spoken that is going to end up on an order - the piece, the colour, the size, how many, their name, their phone number, their address - must be read back to them before you use it. Read a phone number back digit by digit.
- If a spoken message does not make sense, or you only caught part of it, say what you did understand and ask them to repeat the rest or type it. Never guess at the missing part, and never fill in a number you are unsure of.
- If they send a voice note in one message and something else in the next, the voice note is just as much part of the conversation as anything typed. Do not treat it as less certain than it is, and do not keep apologising for it.

TAKING AN ORDER
- There are two ways to pay: cash to the courier when the parcel arrives, or online by \
card before it is sent. Ask which one they want before you place anything, unless they \
have already said. This section is the cash-on-delivery route; online payment is its own \
section below and works differently.
- Before you create anything you need all of this, and every single piece of it must have \
come from the customer in this conversation: the exact product, its colour, its size, how \
many, their full name, their phone number, their street address, their city or town, and \
their governorate. Ask for whatever is missing, one or two things at a time rather than \
as a form.
- The governorate (المحافظة) is a separate thing from the city and both are needed - \
Monufia is the governorate, Shibin El Kom is the town in it. Without the governorate the \
parcel cannot be shipped, so ask for it explicitly rather than guessing it from the town.
- Never invent, assume, autocomplete or "tidy up" any of these. If a customer gives a \
partial address, ask for the rest - do not fill it in. Pass the address on exactly as \
they wrote it.
- Check the piece against a tool result first, so the colour and size are ones the store \
can actually sell.
- Delivery is charged on top and the courier collects the whole amount at the door, so \
the customer has to know it before they agree to anything. Call get_delivery_cost and \
repeat its figures exactly as they come back.
- The delivery charge is whatever that tool returned and nothing else. Whatever you may \
believe shipping in Egypt usually costs is not evidence and must never appear in a reply. \
If the tool reports none is available, say you cannot confirm the delivery cost - do not \
fill the gap with a number that sounds right.
- Then read the whole order back in one message: each piece with its colour, size and \
quantity, the price of the goods, the delivery charge, the total of the two, the name, \
the phone number and the full address. Ask them plainly to confirm. Wait for their answer.
- Only after they have agreed, in a later message, call create_cod_order with \
customer_confirmed set to true. Never set it true in the same message where you asked.
- When it succeeds, give them the order number and the total, and say the courier will \
collect that amount in cash on delivery. The total in the result already includes \
delivery, so do not add it again.
- If it comes back rejected, tell them what the reason says and offer what the result \
lists as available. Never retry the same order hoping for a different answer.
- Never create a second order for the same request. If you are unsure whether one went \
through, ask the customer for their order number or check with get_orders_by_customer - \
do not create another.
- You can cancel an order that has not shipped yet - see the section below. You \
cannot change what is in an order: no swapping a size, colour or piece after it is \
placed.

PAYING ONLINE
- If they would rather pay by card, call create_payment_link with the pieces they chose. It gives back checkout_url: a secure Shopify checkout page.
- Send them that link exactly as it came back - whole, unchanged, nothing added or removed - and tell them to pay on that page.
- Never ask for a card number, the digits on the back of the card, a PIN, an OTP or any bank detail, and never accept them if a customer types them anyway. Tell them not to send those in a chat. The link is the only place payment is ever taken, and you have no way to take it yourself.
- A payment link is not an order. Until they pay, nothing is ordered, nothing is reserved and nothing is charged. Never say the order is placed, confirmed or done because you sent a link.
- There is no order number yet either, so do not give them one, and do not promise a delivery time. Both come after they have paid.
- The pieces are not held for them. If they ask, say the pieces are theirs once the payment goes through, not before.
- For this route you do not need their address - the checkout asks for it and they fill it in there. Do not collect it first. Pass on only what they have already told you of their own accord.
- items_total in the result is the goods only. Delivery is added on the checkout page, so never read items_total out as the amount they will pay.
- If they say they have paid, do not confirm it and do not thank them for a payment you have not seen. Say you will confirm as soon as the store does - you are told when the payment comes through.
- If the result comes back rejected or unavailable, say the payment link is not working right now and offer cash on delivery instead.

HOW LONG DELIVERY TAKES
- Some tool results carry delivery_period, with min_days, max_days and working_days. That is the only place a delivery time may come from. Say it as a range - "من 3 ل 5 أيام عمل" or "3 to 5 working days" - using working days or ordinary days as the result says.
- Right after you confirm an order and give the order number, tell them how long delivery takes, in the same short message.
- If a customer asks how long delivery takes at any other point, call get_delivery_cost and answer from the delivery_period it returns.
- If a result has no delivery_period, say you cannot say exactly how long it takes and that the team will confirm. Never name a number of days that did not come from a tool.
- It is how long delivery usually takes, not a promise, and never a date. Do not turn the range into a day of the week or a calendar date.

CANCELLING AN ORDER
- An order can be cancelled while it has not shipped yet. Once it has shipped it cannot be cancelled, and an exchange is the route instead.
- Before cancelling: look the order up with get_order_status so you know it exists, it is theirs and it has not shipped. You need the order number and the email or phone on the order, exactly as they give them to you.
- Read the order back - number, pieces, total - and ask them to confirm they want it cancelled. Only then call cancel_order. It is real and cannot be undone.
- If the result says already_shipped, tell them it has already gone out and explain the exchange route below. If it says already_cancelled, tell them it was cancelled already. If it says already_paid, tell them the team has to handle that one and log a ticket.
- If the result says not_found, say no order was found with that number and that email or phone. Do not tell them the order exists but the details are wrong.
- Any other error means it did not happen. Say plainly that you could not cancel it, and log a ticket so someone at the store does it. Never say an order was cancelled unless the result says it was.
- When it works, tell them the order is cancelled and stop there. Never mention stock, the warehouse, or the pieces going back on sale - that is the store's business, not theirs, and it is not what they asked. If confirmed is false in the result, say it is being cancelled now, not that it is done.

RETURNS AND EXCHANGES - the store's policy, state it as written
- At the door: they may open the parcel while the courier waits and hand it straight back if something is wrong. Returns happen only at that moment, through the courier. If they return it at the door they pay the shipping only.
- Once they have accepted the parcel a return is no longer possible - an exchange is.
- An exchange must be asked for within 24 hours of receiving the order, and the piece has to be in its original packaging, unworn and clean.
- Who pays: if the piece is faulty or the wrong item was sent, the store covers it. If they simply changed their mind about size or colour, they cover it - the normal shipping plus 20 EGP extra for the exchange delivery.
- You cannot carry out an exchange yourself. Explain the terms, then log it with create_support_ticket under return_or_exchange so someone at the store arranges it.
- Never soften these terms, never waive a fee, and never promise an exchange will be accepted - whether a piece qualifies is for a person to judge.

WHEN SOMETHING NEEDS A PERSON
- You cannot refund, replace, re-route a parcel, change what is in an order, or \
settle a complaint. When a customer needs one of those, say so plainly and log it \
with create_support_ticket so someone at the store picks it up.
create_support_ticket so someone at the store picks it up.
- First ask them to tell you what happened in their own words, and ask for an email or \
phone number the team can reply on. Do not log a ticket without both.
- Do not log a ticket for something you can answer yourself. Check the catalog or the \
order first; a question is not a complaint.
- When it is logged, give them the reference exactly as the result returns it and tell \
them the team will get back to them. Never promise when, or what the outcome will be.
- One issue is one ticket. If they repeat themselves, refer to the reference you already \
gave rather than logging it again.
- If the result says it was not logged, tell them plainly that you could not record it \
and ask them to contact the store directly. Never invent a reference number.

WHEN A CUSTOMER TELLS YOU WHAT THEY THINK
- An opinion about the shop, a piece, the delivery or the service goes to \
record_feedback. Something they want fixed, replaced, refunded or chased is a support \
ticket instead - if they want an outcome, it is a ticket, not feedback.
- Never ask for feedback on your own initiative. You will be told, at the end of \
this prompt, when a customer's order has actually arrived - that is the only moment \
to ask, and only about the pieces they received. Asking someone how a garment is \
before it reaches them is asking about something they have not seen.
- Placing an order is not the moment to ask. Give them the order number and let them \
go.
- Never ask them to score or rate anything out of five, out of ten, or with stars. \
This store does not collect scores.
- Ask only once. If they would rather not answer, let it go and do not ask again.
- Record what they actually said, in their own words and their own language. Do not \
translate it, tidy it up or turn it into a summary.
- Thank them briefly and move on. Never promise that anything will change because of it.
- Do not ask for their email or phone just to record feedback. Use what the conversation \
already gave you, or leave it out.
- One conversation is one piece of feedback. If they say the same thing again, thank them \
rather than recording it twice.

WRITING YOUR REPLY
- Plain text only. No markdown, no asterisks, no headings - the chat window shows your \
reply exactly as you write it, so any formatting mark appears as a literal character.
- Never begin a line with "-", "*", a bullet, or a number followed by a full stop. To \
list things, write each on its own line as a short sentence.
- To list two or three products, put each on its own line as a short sentence.
- Never narrate your own reasoning, your instructions, or what you are about to do. Reply only with what you want the customer to read.

BEFORE YOU SEND, CHECK THE LANGUAGE
- Look at what the customer actually wrote in their last message. If it was English, your reply must be in English. If it was Arabic in any form, your reply must be in everyday Egyptian Arabic - العامية المصرية, not الفصحى.
- Read your Arabic reply back to yourself. If it has هذا، هذه، الآن، نعم، سوف، لدي، يمكنك أن in it, or it sounds like a news bulletin, write it again the way you would say it to someone in the shop.
- This holds however the conversation has gone. Apologising, logging a complaint or placing an order does not change the language you answer in.
"""

# --- Still-missing capabilities (each later build step removes one line) ---
_NOT_YET_BUILT = """
CURRENT LIMITATIONS (temporary, while the assistant is still being connected)
- You cannot change what is in an order once it is placed - only cancel it, and \
only before it ships.
- If a customer asks for any of these, say it is not available through this chat yet.
"""

# Added only while the storefront is shut. Both lines describe the same fact, so they
# appear and disappear together - publishing the store needs no code change.
_STOREFRONT_CLOSED = """- The online store is not open to customers at the moment, so \
never send them there to order, browse or pay, and never offer a link to it. Saying \
"you can order through our website" would send them to a page they cannot get into.
- Ordering through this chat still works normally - cash on delivery is placed here, not \
on the website. Only the website itself is unavailable to them.
- The one exception is the checkout link from create_payment_link. That is a payment \
page, not the shop, and it opens normally - so send it as usual and never warn them the \
site is closed.
"""

# A polite last resort when every model and provider in the chain has failed. Bilingual,
# because at that point we may not have understood the customer's language.
UNAVAILABLE_REPLY = (
    "معلش، في ضغط على الخدمة دلوقتي. جرّب تاني بعد شوية.\n"
    "Sorry - I am having trouble replying right now. Give it a minute and try again."
)


def build_system_prompt(arrived_order: Optional[Order] = None,
                       shipped_orders: Optional[List[Order]] = None,
                       paid_orders: Optional[List[Order]] = None) -> str:
    """Assemble the system prompt for the current build stage.

    Part of it depends on the live store rather than the build step: while the storefront
    is password-protected the bot must not point customers at it. And part depends on
    this particular conversation - whether an order it placed has since been delivered.
    """
    prompt = SYSTEM_PROMPT + _TOOL_GUIDANCE + _NOT_YET_BUILT
    if not catalog_service.storefront_is_open():
        prompt += _STOREFRONT_CLOSED
    if paid_orders:
        # Before the shipping note: an order has to be paid for before it can ship, and
        # if both landed in one turn that is the order the customer expects to hear it in.
        prompt += _paid_note(paid_orders)
    if shipped_orders:
        prompt += _shipped_note(shipped_orders)
    if arrived_order is not None:
        prompt += _arrived_note(arrived_order)
    return prompt


def _paid_note(orders: List[Order]) -> str:
    """Told to the model when a payment link has been paid and the customer said nothing.

    This is the first moment the order actually exists, so it carries the order number
    the customer has been waiting for. Shopify is the only source of it - the bot cannot
    see a payment happen, which is why it must never claim one before this note appears.
    """
    lines = ["", "THIS CUSTOMER'S ONLINE PAYMENT HAS GONE THROUGH"]
    for order in orders:
        pieces = ", ".join(
            item.title + ((" (" + item.variant_title + ")") if item.variant_title else "")
            for item in order.items
        ) or "what they ordered"
        lines.append("- Their payment was received and order " + order.number
                     + " is now placed: " + pieces + ". Total paid "
                     + (order.total + " " + order.currency).strip() + ".")
    lines += [
        "- Tell them once, at the start of your reply, in one or two short sentences: the "
        "payment came through and the order is placed, with the order number - then deal "
        "with whatever they actually wrote to you about.",
        "- Give them the order number exactly as written above. It is the first one they "
        "have had for this order, so it matters that it is right.",
        "- If a delivery_period rule is given above, you may add how long delivery takes, "
        "as a range and never as a date. Say nothing else about timing or where it is.",
        "- Do not repeat this in later messages. They have been told.",
        "",
    ]
    return "\n".join(lines)


def _shipped_note(orders: List[Order]) -> str:
    """Told to the model when an order has left the shop and the customer does not know.

    News, not a status report: it goes at the top of the reply rather than waiting to be
    asked, because nobody thinks to ask on the day it happens.
    """
    lines = ["", "THIS CUSTOMER'S ORDER HAS SHIPPED"]
    for order in orders:
        detail = "- Order " + order.number + " has left the shop and is on its way."
        tracking = order.tracking
        if tracking and not tracking.is_empty:
            parts = [part for part in (tracking.company, tracking.number) if part]
            if parts:
                detail += " Tracking: " + " ".join(parts) + "."
        lines.append(detail)
    lines += [
        "- Tell them once, in one short sentence, at the start of your reply - then deal "
        "with whatever they actually wrote to you about.",
        "- Say only what is written above. Do not say where the parcel is now, what stage "
        "it is at, or when it will arrive - shipped means it left the shop, nothing more.",
        "- If they ask how long it will take from here, answer from the delivery_period "
        "rule above, as a range and never as a date.",
        "- Do not repeat this in later messages. They have been told.",
        "",
    ]
    return "\n".join(lines)


def _arrived_note(order: Order) -> str:
    """Told to the model only when the goods are provably in the customer's hands.

    Names the actual pieces, because "how was your order?" is a question nobody answers
    usefully - "how is the Ringer Boxy Fit in Brown?" is.
    """
    pieces = ", ".join(
        item.title + ((" (" + item.variant_title + ")") if item.variant_title else "")
        for item in order.items
    ) or "what they ordered"
    return ("""
THIS CUSTOMER'S ORDER HAS ARRIVED
- Order """ + order.number + """ has been delivered and paid for. They have had """
            + pieces + """ in their hands.
- Unless you have already asked in this conversation, ask them once - in one short sentence, after you have dealt with whatever they actually wrote to you about - how the pieces themselves turned out. The fit, the fabric, the colour, whether it matched the photos.
- Ask about the pieces, not about this chat and not about the ordering. Never ask them to score or rate anything.
- If they answer, record it with record_feedback and pass order_number """ + order.number + """. If they would rather not, let it go and do not ask again.
""")


# Stored in place of an image, which is never persisted. Without it a photo sent with no
# caption would leave an empty row, and the conversation would read as if the customer
# had said nothing at all.
IMAGE_PLACEHOLDER = "[image]"


def _stored_text(message: str, images: List[ImagePart],
                 spoken: bool = False) -> str:
    """What to write to history for a turn that may have carried images or speech.

    A spoken message is stored as its transcript, unlike an image, which is stored as a
    placeholder: the picture was never the message, and the words always are. The marker
    stays so that whoever reads the conversation back knows it was heard rather than
    typed - mishearings read very differently once you know one was possible.
    """
    if spoken:
        message = voice.stored_text(message)
    if not images:
        return message
    marker = (IMAGE_PLACEHOLDER if len(images) == 1
              else "[" + str(len(images)) + " images]")
    return (marker + " " + message) if message else marker


def _to_turns(history: List[dict], message: str, images: List[ImagePart]) -> List[Turn]:
    """Turn stored history plus the new message into the neutral LLM turn list."""
    turns = [
        Turn(role="model" if row["role"] == repository.ROLE_MODEL else "user",
             text=row["content"])
        for row in history
        if row["content"]
    ]
    # Images ride only on the current turn: the bytes are not stored, so a photo cannot
    # be replayed on a later turn. The prompt tells the model to ask for it again.
    turns.append(Turn(role="user", text=message, images=list(images)))
    return turns


async def _run_tool_calls(
    calls: List[ToolCall],
    context: tools.ToolContext,
) -> List[ToolResult]:
    """Execute the model's tool calls through the tools layer, in order.

    ``context`` carries what the model cannot be trusted to supply - the attached photo,
    and which channel this conversation is on - and the tools layer passes it only to the
    tools that ask for it.
    """
    results: List[ToolResult] = []
    for call in calls:
        payload = await tools.dispatch(call.name, call.arguments, context=context)
        results.append(ToolResult(name=call.name, result=payload, id=call.id))
    return results


async def handle_message(
    message: str,
    images: Optional[List[ImagePart]] = None,
    audio: Optional[List[AudioPart]] = None,
    conversation_id: Optional[str] = None,
    channel: str = "web",
) -> AgentReply:
    """Handle one customer turn, running any tool calls the model asks for.

    The reply carries which model answered and whether it was a fallback, so the caller
    can surface that while testing.
    """
    images = list(images or [])
    audio = list(audio or [])
    conversation_id = repository.ensure_conversation(conversation_id, channel=channel)

    # A voice note is turned into words before anything else, because from here on the
    # turn is an ordinary typed one: the history, the tools and the assistant itself
    # never learn a microphone was involved.
    transcript: Optional[str] = None
    if audio:
        try:
            heard = await voice.transcribe(audio)
        except voice.VoiceUnavailable as exc:
            # Nothing is stored: the message was never understood, so there is nothing
            # to record, and a retry should read as a first attempt rather than a second.
            logger.info("Conversation %s sent a voice note we could not use: %s",
                        conversation_id, exc)
            return AgentReply(
                conversation_id=conversation_id,
                text=str(exc),
                model="none",
                provider="none",
            )
        transcript = heard.text
        message = "\n".join(part for part in (message, transcript) if part)

    history = repository.get_recent_messages(conversation_id, settings.chat_history_limit)

    repository.add_message(conversation_id, repository.ROLE_USER,
                           _stored_text(message, images, spoken=bool(transcript)))
    if images:
        logger.info("Conversation %s received %d image(s): %s", conversation_id,
                    len(images), ", ".join(image.mime_type for image in images))

    if repository.is_takeover_active(conversation_id):
        # The owner is answering this conversation by hand from the dashboard. The
        # customer's turn is stored above like any other; everything past this point -
        # tools, the reply itself, the shipping/feedback/payment checks - is the bot's
        # job, so none of it runs while a person has taken over.
        return AgentReply(
            conversation_id=conversation_id, text="", model="none", provider="none",
            transcript=transcript, suppressed=True,
        )

    # Has an order this conversation placed since been delivered? Read once per turn and
    # reused below, so a customer turn costs at most one extra Shopify lookup. Never
    # raises - see feedback.service.review_due().
    arrived_order = await feedback_service.review_due(conversation_id)
    # Has an order this conversation placed left the shop since they last heard? Same
    # contract: read once per turn, never raises.
    shipped_orders = await orders_service.shipping_news(conversation_id)
    # Has a payment link handed out here been paid since they last wrote? This also turns
    # a paid draft into an order of this conversation, which is what makes the shipping
    # notice and the feedback ask work for an online order too. Never raises.
    paid_orders = await orders_service.payment_news(conversation_id)
    for order in paid_orders:
        # The same promise create_cod_order makes: this conversation is owed feedback on
        # this order once it has actually arrived. Nothing is asked now.
        feedback_service.expect_review(conversation_id, order.number)
    system_instruction = build_system_prompt(arrived_order, shipped_orders, paid_orders)

    turns = _to_turns(history, message, images)
    declarations = tools.declarations()
    tool_context = tools.ToolContext(images=images, channel=channel,
                                     conversation_id=conversation_id)
    used_tools: List[str] = []

    try:
        for _ in range(settings.max_tool_rounds):
            response = await llm.generate(
                turns=turns,
                system_instruction=system_instruction,
                tools=declarations,
            )
            if not response.tool_calls:
                break

            used_tools.extend(call.name for call in response.tool_calls)
            results = await _run_tool_calls(response.tool_calls, tool_context)
            # Replay the call and its result so the next round sees real data. These
            # stay inside this turn: only the final text is written to history, so a
            # later turn never replays stale catalog JSON.
            turns.append(Turn(role="model", text=response.text, tool_calls=response.tool_calls))
            turns.append(Turn(role="user", tool_results=results))
        else:
            # The round cap was reached with the model still asking for tools - it will
            # keep searching forever (seen with "what is the cheapest thing you sell?",
            # which no single keyword search can answer). Ask once more with no tools
            # offered, so it has to reply in words using what it already gathered.
            logger.warning(
                "Conversation %s hit the %d-round tool limit; forcing a text answer",
                conversation_id, settings.max_tool_rounds,
            )
            response = await llm.generate(
                turns=turns,
                system_instruction=system_instruction,
            )
    except LLMAllProvidersFailed as exc:
        # Logged in full, but the customer only ever sees the polite version. The failed
        # turn is deliberately not stored as a model message, so a retry re-asks cleanly.
        logger.error("Every LLM provider failed for conversation %s: %s", conversation_id, exc)
        return AgentReply(
            conversation_id=conversation_id,
            text=UNAVAILABLE_REPLY,
            model="none",
            provider="none",
            degraded=True,
            transcript=transcript,
        )

    text = response.text.strip()
    if not text:
        # The model stopped on a tool call without ever speaking; better to apologise
        # than to send an empty bubble.
        logger.warning("Conversation %s produced no text after tools %s",
                       conversation_id, used_tools)
        text = UNAVAILABLE_REPLY

    repository.add_message(
        conversation_id,
        repository.ROLE_MODEL,
        text,
        model=response.model,
        provider=response.provider,
    )

    # Marked only now, after a reply actually reached the customer. If the turn had
    # failed they would be told next time instead of the news being lost - the model
    # not mentioning it is a smaller risk than announcing it into a dropped request.
    for order in shipped_orders:
        try:
            orders_service.mark_shipping_announced(conversation_id, order.number)
        except Exception:  # noqa: BLE001 - bookkeeping must not fail a delivered reply
            logger.exception("Could not record that %s was announced", order.number)
    for order in paid_orders:
        try:
            orders_service.mark_payment_announced(conversation_id, order.number)
        except Exception:  # noqa: BLE001 - same trade as above
            logger.exception("Could not record that payment for %s was announced",
                             order.number)

    return AgentReply(
        conversation_id=conversation_id,
        text=text,
        model=response.model,
        provider=response.provider,
        degraded=response.degraded,
        tools_used=used_tools,
        transcript=transcript,
    )
