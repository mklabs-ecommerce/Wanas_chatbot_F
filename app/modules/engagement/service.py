"""Engagement module - the public surface for Instagram comments and DMs.

What this module decides: what a public comment is, and what the shop does about it.
What it deliberately does not decide: anything a customer is told in conversation. An
important comment is handed to ``chat.service.handle_message()``, the same door the web
widget goes through, so there is one assistant with one set of honesty rules rather than
a second one growing quietly inside a channel adapter.

Four rules run through the whole file:

**The public wording is fixed.** A public reply is permanent, visible to everyone, and
reflects on the brand. Nothing a model writes goes into a comment thread. The same holds
for the DM opener: it is the first thing a stranger reads from the shop, and the bot has
no way to start a conversation anyway - ``handle_message()`` answers a customer turn, it
does not speak first.

**Silence is the safe failure.** When the classifier cannot be reached, when the catalog
is down, when anything is uncertain - nothing is posted, nothing is liked, nobody is
messaged. A missing reply costs one customer; a wrong public one is read by everyone.

**Nothing outward happens twice.** Every event is claimed before it is worked on, and a
claim is only given back when the failure happened before any outward call. Meta gives a
comment exactly one private reply for all time; spending it on a retry is unrecoverable.

**The dry run is real.** With ``INSTAGRAM_DRY_RUN`` on, every path runs and decides, and
the last step is a log line instead of a call. That is how the first live run gets read
before it is public.
"""

import json
import logging
import re
from datetime import datetime
from typing import List, Optional, Tuple

from app.core.config import settings
from app.integrations import llm
from app.integrations.instagram.client import (
    InstagramClient,
    InstagramError,
    InstagramNotConfigured,
    InstagramRejected,
)
from app.integrations.llm_types import ImagePart, LLMError, Turn
from app.modules.catalog import service as catalog_service
from app.modules.chat import service as chat_service
from app.modules.chat.attachments import AttachmentError
from app.modules.engagement import repository
from app.modules.engagement.schemas import (
    CLASSES,
    DEFAULT_CLASS,
    IMPORTANT,
    NEGATIVE,
    NEITHER,
    POSITIVE,
    Classification,
    CommentEvent,
    MessageEvent,
    PostProduct,
    parse_webhook,
)
from app.modules.support import service as support_service
from app.modules.support.service import TicketRejected

logger = logging.getLogger(__name__)

CHANNEL = "instagram"

# --- what the public sees -------------------------------------------------
# Fixed wording, in both languages the shop speaks. Short on purpose: a public reply
# acknowledges, it does not explain - the explaining happens in the DM.

PUBLIC_REPLY_AR = "شكراً لتواصلك! بعتنالك رسالة خاصة 💬"
PUBLIC_REPLY_EN = "Thanks for reaching out! We have sent you a DM 💬"

OPENER_AR = "أهلاً بيك! شفنا كومنتك على بوست {product} 💬 تحب أساعدك في إيه؟"
OPENER_AR_PLAIN = "أهلاً بيك! شفنا كومنتك على البوست 💬 تحب أساعدك في إيه؟"
OPENER_EN = "Hi! We saw your comment on the {product} post 💬 How can I help?"
OPENER_EN_PLAIN = "Hi! We saw your comment on the post 💬 How can I help?"

# How a comment appears in the conversation history, so whoever reads it back knows the
# customer said this in public rather than in the chat.
COMMENT_PREFIX = "[comment]"

# Attachment kinds the assistant cannot read at all. Skipped before they are downloaded,
# and told apart from photos and voice notes because the customer deserves the right
# reason: an mp4 sniffs as an audio container, and "that recording is too short to hear"
# is a baffling thing to hear back about a video you just sent.
_UNREADABLE_KINDS = {"video", "share", "story", "story_mention", "reel",
                     "template", "fallback", "location", "file"}

_ARABIC = re.compile(r"[؀-ۿ]")
_MENTION = re.compile(r"@[\w.]+")
# Emoji, symbols, punctuation and whitespace - everything that is not a word.
_WORDLIKE = re.compile(r"[0-9A-Za-z؀-ۿ]")

# A comment made only of these is a reaction, and gets a like without spending a model
# request. Kept small and unambiguous on purpose - anything not on the list goes to the
# classifier rather than being guessed at.
_POSITIVE_EMOJI = set("❤🧡💛💚💙💜🖤🤍🤎♥😍🥰😘😻👏🙌🔥✨💯👌👍🤩💖💕💗💓💞💘😊😁😄🤗🥳💐🌹")
_NEGATIVE_EMOJI = set("💔😡🤬👎🤮🙄😒😤😠💩")


# --- receiving ------------------------------------------------------------


def accept(payload: dict, background) -> None:
    """Take one webhook delivery and schedule the work in it.

    Runs inside the request, so it must stay cheap: parse, drop what is ours, claim what
    is left, and hand each event to a background task. The claim happens here rather
    than in the task because Meta redelivers on a slow response, and a redelivery that
    arrives while the first is still running must lose.
    """
    events = parse_webhook(payload)
    if not events:
        return

    if not settings.instagram_enabled:
        logger.info("Instagram is off; ignoring %d webhook event(s)", len(events))
        return

    our_id = settings.instagram_business_account_id
    for event in events:
        if isinstance(event, CommentEvent):
            if event.author_id and event.author_id == our_id:
                # Our own public reply comes back as a comment on our own post. Without
                # this the shop answers itself until the rate limiter stops it.
                continue
            if not repository.claim(event.comment_id, repository.KIND_COMMENT):
                continue
            background.add_task(handle_comment, event)
        elif isinstance(event, MessageEvent):
            if event.is_echo or (event.sender_id and event.sender_id == our_id):
                # Meta echoes our own sends on the same webhook.
                continue
            if not repository.claim(event.message_id, repository.KIND_MESSAGE):
                continue
            background.add_task(handle_direct_message, event)


# --- comments -------------------------------------------------------------


async def handle_comment(event: CommentEvent) -> str:
    """Decide what a comment is and act on it. Returns what was done, for the log.

    Never raises: this runs detached from any request, and an unhandled error here would
    leave a claimed event with no record of why nothing happened.
    """
    try:
        classification = await classify_comment(event.text)
    except Exception:  # noqa: BLE001 - classifying must never take the process down
        logger.exception("Could not classify comment %s", event.comment_id)
        repository.finish(event.comment_id, repository.OUTCOME_FAILED,
                          action="classification failed")
        return "failed"

    logger.info("Comment %s from %s: %s (%s: %s)", event.comment_id, event.handle,
                classification.kind, classification.source, classification.reason)

    try:
        if classification.kind == POSITIVE:
            action = await _do_like(event)
        elif classification.kind == IMPORTANT:
            action = await _do_handoff(event)
        elif classification.kind == NEGATIVE:
            action = await _do_log_negative(event)
        else:
            action = "no action"
    except Exception:  # noqa: BLE001 - same contract as above
        logger.exception("Could not act on comment %s", event.comment_id)
        repository.finish(event.comment_id, repository.OUTCOME_FAILED,
                          classification=classification.kind, action="action failed")
        return "failed"

    outcome = repository.OUTCOME_DONE if classification.acts_publicly else repository.OUTCOME_SKIPPED
    repository.finish(event.comment_id, outcome,
                      classification=classification.kind, action=action)
    return action


async def _do_like(event: CommentEvent) -> str:
    """The positive path. The lowest-risk thing the shop can do in public."""
    if _dry_run():
        return _dry("would like this comment")
    client = _client()
    if client is None:
        return "not configured"
    try:
        await client.like_comment(event.comment_id)
    except InstagramError as exc:
        logger.warning("Could not like comment %s: %s", event.comment_id, exc)
        return "like failed: " + str(exc)
    return "liked"


async def _do_handoff(event: CommentEvent) -> str:
    """The important path: open the DM first, then say so in public.

    The order is the point. The public reply promises a DM, so it must not be published
    if the DM did not go. A DM with no public reply is merely quiet; a public promise
    with no DM is the shop saying something untrue where everyone can read it.
    """
    product = await resolve_post_product(event.media_id)
    opener = _opener(event.text, product)

    if _dry_run():
        return _dry("would DM " + event.handle + ": " + opener
                    + (" | then reply publicly" if settings.instagram_public_replies else ""))

    client = _client()
    if client is None:
        return "not configured"

    try:
        sent = await client.send_private_reply(event.comment_id, opener)
    except InstagramRejected as exc:
        if exc.private_reply_already_sent:
            # Our own database says this comment is new, so this is a replay after a
            # crash or a restore. The DM was already sent; saying so publicly a second
            # time would be the only visible mistake left to make.
            logger.info("Comment %s already had its private reply", event.comment_id)
            return "private reply already spent"
        logger.warning("Instagram refused the private reply for %s: %s",
                       event.comment_id, exc)
        return "private reply refused: " + str(exc)
    except InstagramError as exc:
        logger.warning("Could not send the private reply for %s: %s", event.comment_id, exc)
        return "private reply failed: " + str(exc)

    # The DM exists, so the conversation should carry what led to it. Stored as what it
    # is - something said in public, then answered in private.
    conversation_id = ""
    try:
        conversation_id = chat_service.seed_conversation(
            customer_text=COMMENT_PREFIX + " " + event.text.strip(),
            assistant_text=opener,
            channel=CHANNEL,
        )
        _remember_thread(event, conversation_id, str(sent.get("recipient_id") or ""))
    except Exception:  # noqa: BLE001 - the DM is sent; bookkeeping must not undo that
        logger.exception("Could not record the conversation opened by comment %s",
                         event.comment_id)

    sent_publicly = False
    if settings.instagram_public_replies:
        try:
            await client.reply_to_comment(event.comment_id, _public_reply(event.text))
            sent_publicly = True
        except InstagramError as exc:
            # The DM went; the acknowledgement did not. Nothing to undo and nothing
            # untrue was said, so this is a warning and not a failure.
            logger.warning("Could not reply publicly to %s: %s", event.comment_id, exc)

    return ("DM sent" + (" + public reply" if sent_publicly else "")
            + (" (conversation " + conversation_id + ")" if conversation_id else ""))


def _remember_thread(event: CommentEvent, conversation_id: str, igsid: str) -> None:
    """Attach the commenter to the conversation their comment opened.

    The id that identifies someone in a comment is not the id that identifies them in a
    DM, so the two could not be joined from the comment alone. The private reply's own
    response carries ``recipient_id``, which *is* the messaging id - so the moment the
    DM is sent, their next message already knows where it belongs, and they keep one
    conversation with one history rather than starting again.

    Without it the link is keyed on the comment and their reply opens a second
    conversation: worth logging loudly, because the customer would be asked things they
    have already answered.
    """
    if not igsid:
        logger.warning("Instagram did not return a recipient id for comment %s; the "
                       "customer's reply will start a fresh conversation",
                       event.comment_id)
        igsid = "comment:" + event.comment_id
    repository.link_thread(
        igsid=igsid,
        conversation_id=conversation_id,
        username=event.username,
        opened_from_comment=event.comment_id,
    )


async def _do_log_negative(event: CommentEvent) -> str:
    """The negative path: no public reply, no like, no DM - a ticket and nothing else.

    ``contact`` is their Instagram handle, which is truthful: it is how the owner
    reaches them, and it satisfies the ticket's contact requirement without relaxing a
    guard that exists so a complaint can actually be answered.
    """
    if _dry_run():
        return _dry("would log a ticket for " + event.handle)

    summary = ("Negative public comment on Instagram from " + event.handle + ": "
               + event.text.strip())
    try:
        ticket = await support_service.create_ticket(
            category="complaint",
            summary=summary,
            customer_name=event.username,
            contact="instagram:" + event.handle,
            channel=CHANNEL,
        )
    except TicketRejected as exc:
        logger.info("Could not log a ticket for comment %s: %s", event.comment_id, exc)
        return "ticket rejected: " + str(exc)
    return "ticket " + ticket.reference


# --- classifying ----------------------------------------------------------

_CLASSIFY_SYSTEM = """You sort comments left on an Egyptian clothing shop's Instagram \
posts. The shop replies to some of them automatically, so your answer decides what \
happens in public.

Choose exactly one:
- "important": the person is asking the shop something or wants something from it - \
price, size, availability, colours, fabric, how to order, delivery, an order they placed, \
a complaint they want fixed. A question aimed at the shop is important even if it is \
short.
- "positive": praise, admiration, hearts, or a reaction with nothing being asked.
- "negative": criticism, anger, an accusation, or an insult, with nothing being asked of \
the shop.
- "neither": spam, promotion of something else, a comment aimed at another person, or \
anything you cannot read.

Rules:
- A comment that is essentially just tagging a friend ("@sara شوفي دي") is "neither". \
The person tagging is pointing a friend at the post, not asking the shop anything. If \
they also ask something themselves, that is "important".
- Egyptian Arabic, Arabizi and English all appear. Judge what is meant, not how it is \
spelled.
- When you cannot tell, answer "neither". The shop does nothing at all for that, which \
is the safe outcome.

Reply with JSON only: {"class": "important|positive|negative|neither", "reason": "a few \
words"}"""


async def classify_comment(text: str) -> Classification:
    """Work out what a comment is.

    The cheap answers come first and cost nothing. That matters more here than it looks:
    the store runs on a free tier measured in tens of requests per window, comments
    arrive whether or not a customer is mid-conversation, and a model request spent on a
    row of hearts is a request not available to answer someone.
    """
    stripped = (text or "").strip()
    if not stripped:
        return Classification(kind=NEITHER, reason="empty comment", source="rule")

    without_mentions = _MENTION.sub("", stripped).strip()
    if not _WORDLIKE.search(without_mentions):
        # Nothing but mentions, emoji and punctuation once the handles are removed.
        if _MENTION.search(stripped) and not without_mentions:
            return Classification(kind=NEITHER, reason="only tags another person",
                                  source="rule")
        symbols = set(without_mentions)
        if symbols & _NEGATIVE_EMOJI:
            return Classification(kind=NEGATIVE, reason="unhappy reaction", source="rule")
        if symbols & _POSITIVE_EMOJI:
            return Classification(kind=POSITIVE, reason="emoji reaction", source="rule")
        return Classification(kind=NEITHER, reason="no words to read", source="rule")

    try:
        response = await llm.generate(
            turns=[Turn(role="user", text=stripped)],
            system_instruction=_CLASSIFY_SYSTEM,
            json_output=True,
            temperature=0.0,
            model=settings.gemini_classifier_model or None,
        )
    except LLMError as exc:
        # Nothing at all happens in public. The comment stays unanswered, which is what
        # would have happened before any of this existed.
        logger.warning("Could not classify a comment: %s", exc)
        return Classification(kind=DEFAULT_CLASS, reason="classifier unavailable",
                              source="default")

    parsed = _parse_class(response.text)
    if parsed is None:
        logger.warning("Classifier returned nothing usable: %r", (response.text or "")[:200])
        return Classification(kind=DEFAULT_CLASS, reason="unreadable classification",
                              source="default")
    return parsed


def _parse_class(text: str) -> Optional[Classification]:
    """Read the classifier's JSON, tolerating a code fence around it."""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?|```$", "", candidate, flags=re.MULTILINE).strip()
    try:
        data = json.loads(candidate)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    kind = str(data.get("class") or "").strip().lower()
    if kind not in CLASSES:
        return None
    return Classification(kind=kind, reason=str(data.get("reason") or "")[:200])


# --- which product a post is about ----------------------------------------


async def resolve_post_product(media_id: str) -> PostProduct:
    """Work out which catalog piece a post is showing, once per post.

    Two attempts, cheapest first. The caption usually names the piece, and matching it
    costs nothing but a cached catalog read. Only when that is inconclusive is the post's
    own image shown to a model - through ``catalog.identify_product_from_image()``, the
    same function and the same earned-confidence rules a customer's photo goes through.

    An unresolved post is cached as unresolved. The bot then asks which piece they meant,
    which is the honest answer and a much smaller cost than guessing.
    """
    if not media_id:
        return PostProduct(media_id="")

    cached = repository.post_product(media_id)
    if cached is not None:
        return cached

    resolved = PostProduct(media_id=media_id)
    client = _client()
    media = {}
    if client is not None:
        try:
            media = await client.get_media(media_id)
        except InstagramError as exc:
            logger.info("Could not read post %s: %s", media_id, exc)

    caption = str(media.get("caption") or "")
    try:
        title = await _match_caption(caption)
        if title:
            resolved = PostProduct(media_id=media_id, title=title, source="caption")
        elif client is not None:
            resolved = await _match_image(media_id, media, client)
    except Exception:  # noqa: BLE001 - an unresolved post is a fine answer
        logger.exception("Could not resolve the product for post %s", media_id)
        return PostProduct(media_id=media_id)

    repository.save_post_product(resolved)
    if resolved.resolved:
        logger.info("Post %s is showing %s (by %s)", media_id, resolved.title, resolved.source)
    else:
        logger.info("Post %s could not be matched to a product", media_id)
    return resolved


async def _match_caption(caption: str) -> str:
    """The catalog piece a caption names, when it names exactly one.

    Deliberately strict: a caption that could be two products resolves to neither. Half
    a match is worse than none, because it becomes the piece the bot assumes they meant.
    """
    caption = (caption or "").strip()
    if not caption:
        return ""
    try:
        matches = await catalog_service.search_products(caption, limit=3)
    except Exception as exc:  # noqa: BLE001 - Shopify being down costs the context only
        logger.info("Could not match a caption against the catalog: %s", exc)
        return ""
    if len(matches) != 1:
        return ""
    return matches[0].title


async def _match_image(media_id: str, media: dict, client: InstagramClient) -> PostProduct:
    """Show the post's own picture to the matcher a customer's photo goes through."""
    url = media.get("thumbnail_url") or media.get("media_url")
    if not url:
        return PostProduct(media_id=media_id)

    try:
        data = await client.download(str(url))
    except InstagramError as exc:
        logger.info("Could not download the image for post %s: %s", media_id, exc)
        return PostProduct(media_id=media_id)

    from app.modules.chat.attachments import sniff_mime_type

    mime = sniff_mime_type(data)
    if mime is None:
        return PostProduct(media_id=media_id)

    identification = await catalog_service.identify_product_from_image(
        [ImagePart(data=data, mime_type=mime)])
    # Only a confident match is stored. The rules behind that flag are the ones written
    # for customer photos, and they were written because the bot once announced the
    # wrong product from its own description of a picture.
    if not identification.confident or not identification.matches:
        return PostProduct(media_id=media_id)

    best = identification.matches[0]
    return PostProduct(media_id=media_id, title=best.product.title,
                       color=best.color or "", source="image")


# --- direct messages ------------------------------------------------------


async def handle_direct_message(event: MessageEvent) -> str:
    """Answer one Instagram DM with the ordinary assistant.

    Everything the bot can do on the web works here unchanged - the catalog, orders,
    payment links, photos, voice notes - because this hands the turn to the same door
    and only the transport differs.

    Never raises, for the same reason ``handle_comment`` does not.
    """
    conversation_id = _conversation_for(event)

    try:
        files = await _download_attachments(event)
    except Exception:  # noqa: BLE001
        logger.exception("Could not fetch the attachments on message %s", event.message_id)
        files = []

    if not event.text.strip() and not files:
        if event.attachments:
            # They sent something - a video, a shared post, a story reaction - and it
            # carried no words. Saying so is better than answering nothing at all, which
            # reads as being ignored.
            await _say(event, "I can read photos and voice notes, but not that kind of "
                              "file. Could you send a photo or tell me in a message?")
            repository.finish(event.message_id, repository.OUTCOME_SKIPPED,
                              action="attachment we cannot read")
            return "nothing to answer"
        # A sticker or a reaction with nothing attached at all. Nothing was said.
        repository.finish(event.message_id, repository.OUTCOME_SKIPPED,
                          action="nothing readable in the message")
        return "nothing to answer"

    try:
        answer = await chat_service.handle_message(
            text=event.text,
            raw_attachments=files,
            conversation_id=conversation_id,
            channel=CHANNEL,
        )
        reply, new_conversation = answer.text, answer.conversation_id
        suppressed = answer.suppressed
    except AttachmentError as exc:
        # Written to be read by a customer, so it is sent on as the reply.
        reply, new_conversation, suppressed = str(exc), conversation_id or "", False
    except Exception:  # noqa: BLE001
        logger.exception("Could not answer Instagram message %s", event.message_id)
        repository.finish(event.message_id, repository.OUTCOME_FAILED,
                          action="the assistant failed")
        return "failed"

    if new_conversation:
        repository.link_thread(event.sender_id, new_conversation)
        repository.record_inbound(event.sender_id)

    if suppressed:
        # The owner has taken this conversation over from the dashboard - the customer's
        # message is stored (above) but the bot must not also answer it.
        repository.finish(event.message_id, repository.OUTCOME_SKIPPED,
                          action="owner is handling this conversation")
        return "owner handling"

    if _dry_run():
        repository.finish(event.message_id, repository.OUTCOME_SKIPPED,
                          action=_dry("would reply: " + reply[:400]))
        return "dry run"

    client = _client()
    if client is None:
        repository.finish(event.message_id, repository.OUTCOME_SKIPPED,
                          action="not configured")
        return "not configured"

    try:
        await client.send_message(event.sender_id, reply)
    except InstagramError as exc:
        # The turn is already in the history, so a resend would double it. Better to
        # lose one reply than to have the conversation remember something twice.
        logger.warning("Could not deliver the reply to %s: %s", event.sender_id, exc)
        repository.finish(event.message_id, repository.OUTCOME_FAILED,
                          action="delivery failed: " + str(exc))
        return "delivery failed"

    repository.finish(event.message_id, repository.OUTCOME_DONE, action="replied")
    return "replied"


async def _say(event: MessageEvent, text: str) -> bool:
    """Send one message back, honouring the dry run. Never raises."""
    if _dry_run():
        _dry("would reply to " + event.sender_id + ": " + text)
        return False
    client = _client()
    if client is None:
        return False
    try:
        await client.send_message(event.sender_id, text)
    except InstagramError as exc:
        logger.warning("Could not reply to %s: %s", event.sender_id, exc)
        return False
    return True


def username_for_conversation(conversation_id: str) -> Optional[str]:
    """This conversation's Instagram handle, or None if it isn't one / never captured."""
    thread = repository.thread_for_conversation(conversation_id)
    return (thread or {}).get("username") or None


async def send_owner_reply(conversation_id: str, text: str) -> bool:
    """Send the owner's own words to this conversation's Instagram thread.

    Mirrors the rest of this module's sends: honours the dry run, is never retried, and
    - the one rule that matters here - the message is stored in the conversation
    (``chat_service.post_owner_message``, which also pauses the bot for this
    conversation) only *after* a confirmed or dry-run-simulated send. The dashboard must
    never show a message as delivered that Instagram never received.
    """
    thread = repository.thread_for_conversation(conversation_id)
    if thread is None:
        logger.warning("No Instagram thread for conversation %s; cannot send owner reply",
                       conversation_id)
        return False
    if _dry_run():
        chat_service.post_owner_message(conversation_id, text)
        _dry("would send owner reply to " + thread["igsid"] + ": " + text)
        return True
    client = _client()
    if client is None:
        return False
    try:
        await client.send_message(thread["igsid"], text)
    except InstagramError as exc:
        logger.warning("Could not deliver owner reply for %s: %s", conversation_id, exc)
        return False
    chat_service.post_owner_message(conversation_id, text)
    return True


def _conversation_for(event: MessageEvent) -> Optional[str]:
    """The conversation this person is already in, if any.

    Someone who was DM'd off a comment is already here, keyed on the messaging id the
    private reply handed back. Everyone else is new, and ``handle_message`` makes them a
    conversation.
    """
    existing = repository.thread(event.sender_id)
    if existing:
        return existing["conversation_id"]
    return None


async def _download_attachments(event: MessageEvent) -> List[Tuple[bytes, Optional[str]]]:
    """Fetch what they attached. A file we cannot fetch is simply not there."""
    if not event.attachments:
        return []
    client = _client()
    if client is None:
        return []

    files: List[Tuple[bytes, Optional[str]]] = []
    for attachment in event.attachments:
        if not attachment.url:
            continue
        if (attachment.kind or "").lower() in _UNREADABLE_KINDS:
            # Meta's kind is structural rather than a guessed content type, so it is
            # trustworthy for this - and skipping it saves the download too.
            logger.info("Ignoring a %s attachment - nothing readable in it",
                        attachment.kind)
            continue
        try:
            data = await client.download(attachment.url)
        except InstagramError as exc:
            logger.info("Could not download a %s attachment: %s", attachment.kind, exc)
            continue
        # The declared kind is passed along as advisory only - the bytes are sniffed on
        # the way in, the same as every other upload.
        files.append((data, None))
    return files


# --- wording --------------------------------------------------------------


def _public_reply(comment: str) -> str:
    return PUBLIC_REPLY_AR if _is_arabic(comment) else PUBLIC_REPLY_EN


def _opener(comment: str, product: PostProduct) -> str:
    """The DM opener, naming the piece when the post could be matched.

    It names the product and never its price: the cached match answers which piece, and
    what it costs is always a live lookup the assistant makes when they ask.
    """
    piece = product.describe() if product else ""
    if _is_arabic(comment):
        return OPENER_AR.format(product=piece) if piece else OPENER_AR_PLAIN
    return OPENER_EN.format(product=piece) if piece else OPENER_EN_PLAIN


def _is_arabic(text: str) -> bool:
    """Whether to open in Arabic.

    Arabic script decides it. Arabizi comes out as English here, which is a deliberate
    limit of a fixed template - the assistant switches to however they write back, and
    that is one message later.
    """
    return bool(_ARABIC.search(text or ""))


# --- plumbing -------------------------------------------------------------


def _client() -> Optional[InstagramClient]:
    """A client, or None when Instagram is not configured."""
    try:
        return InstagramClient()
    except InstagramNotConfigured as exc:
        logger.warning("Instagram is not configured: %s", exc)
        return None


def _dry_run() -> bool:
    return settings.instagram_dry_run


def _dry(what: str) -> str:
    logger.info("DRY RUN: %s", what)
    return "dry run: " + what


def recent_activity(limit: int = 50) -> List[dict]:
    """What has been handled lately, for the owner-facing view."""
    return repository.recent_events(limit)


def comments_received_in_range(start: datetime, end: datetime) -> int:
    """How many comments arrived in ``[start, end)``. For admin.analytics' funnel KPI."""
    return repository.comment_count_in_range(start, end)


def dms_opened_from_comments_in_range(start: datetime, end: datetime) -> int:
    """How many DM threads a comment opened in ``[start, end)``. For the same funnel."""
    return repository.opened_thread_count_in_range(start, end)
