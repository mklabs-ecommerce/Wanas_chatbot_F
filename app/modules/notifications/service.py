"""Telling the store owner that something needs them.

Email today; the module exists so that adding another channel later (SMS, a Slack hook)
is a change here rather than in whatever raised the alert.

The rule this module is built around: **a notification failure must never lose the thing
it was announcing.** The ticket is already stored by the time we are called, so every
failure path here logs loudly and returns False. Nothing raises.

The same rule governs the extra context the email carries. Reading the order back from
Shopify and the conversation back from the database both make the email far more useful,
and both can fail - so each is gathered defensively and its absence downgrades the email
rather than losing it.
"""

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import List, Optional

from app.core.config import settings
from app.modules.chat import service as chat_service
from app.modules.feedback.schemas import Feedback
from app.modules.orders import service as orders_service
from app.modules.orders.schemas import Order
from app.modules.support.schemas import Ticket

logger = logging.getLogger(__name__)

# Long enough to see how the issue arose, short enough to read in a mail client.
TRANSCRIPT_LIMIT = 20
# A customer can paste a great deal into a chat window; the email needs to stay readable.
MAX_TRANSCRIPT_CHARS = 4000


async def notify_new_ticket(ticket: Ticket) -> bool:
    """Email the store owner about a new support ticket.

    Returns whether it was sent. False is a normal outcome when email is not configured -
    the ticket is in the database either way, and it is logged so it is not invisible.
    """
    if not settings.email_configured:
        # Deliberately warning, not debug: an unconfigured mailbox means real customer
        # problems are piling up where nobody is looking.
        logger.warning(
            "No SMTP configured - ticket %s (%s) stored but nobody was emailed. "
            "Contact: %s", ticket.reference, ticket.category, ticket.contact or "none",
        )
        return False

    order = await _order_for(ticket)
    message = _build(ticket, order=order, transcript=_transcript_for(ticket))
    try:
        # smtplib is blocking, and this runs inside the request that is answering a
        # customer; a slow mail server must not hold up their reply.
        await asyncio.to_thread(_send, message)
    except Exception as exc:  # noqa: BLE001 - a failed email must not break the chat
        logger.exception("Could not email ticket %s: %s", ticket.reference, exc)
        return False

    logger.info("Emailed ticket %s to %s", ticket.reference, settings.store_owner_email)
    return True


async def notify_new_feedback(feedback: Feedback) -> bool:
    """Email the store owner about unhappy feedback.

    Only ever called for negative feedback - that is the owner's decision (2026-08-19),
    and it is enforced in ``feedback.service``, not here. This function does not
    second-guess it; if it is called, it sends.

    Same contract as ``notify_new_ticket``: the feedback is already stored, so every
    failure path logs and returns False. Nothing raises.
    """
    if not settings.email_configured:
        logger.warning(
            "No SMTP configured - %s feedback stored but nobody was emailed. Contact: %s",
            feedback.sentiment, feedback.contact or "none",
        )
        return False

    order = await _order_for(feedback)
    message = _build_feedback(feedback, order=order, transcript=_transcript_for(feedback))
    try:
        await asyncio.to_thread(_send, message)
    except Exception as exc:  # noqa: BLE001 - a failed email must not break the chat
        logger.exception("Could not email feedback from conversation %s: %s",
                         feedback.conversation_id or "unknown", exc)
        return False

    logger.info("Emailed %s feedback to %s", feedback.sentiment, settings.store_owner_email)
    return True


async def notify_order_cancelled(order: Order, reason: str = "",
                                 conversation_id: str = "") -> bool:
    """Tell the store owner that the bot cancelled an order.

    The owner's decision (2026-08-19): the bot cancels unshipped orders itself, and the
    owner is told every time. This is the only outward action the bot takes that a person
    might need to undo, so it is never silent.

    Same contract as the others: the cancellation already happened, so every failure path
    logs and returns False. Nothing raises.
    """
    if not settings.email_configured:
        logger.warning("No SMTP configured - order %s was cancelled but nobody was emailed.",
                       order.number)
        return False

    message = EmailMessage()
    message["Subject"] = "[Cancelled] order " + order.number
    message["From"] = settings.smtp_from
    message["To"] = settings.store_owner_email

    lines = [
        "The assistant cancelled an order at the customer's request.",
        "",
        "Order:      " + order.number,
        "Placed:     " + (order.placed_on or "unknown"),
        "Status now: " + order.status_words,
        "Total was:  " + _money(order.total, order.currency),
    ]
    if order.phone or order.email:
        lines.append("Customer:   " + ", ".join(
            part for part in (order.phone, order.email) if part))
    if order.ships_to_city or order.ships_to_country:
        lines.append("Was for:    " + ", ".join(
            part for part in (order.ships_to_city, order.ships_to_country) if part))
    lines.append("")
    lines.append("Reason given: " + (reason.strip() or "none given"))
    lines.append("")
    lines.append("The items were restocked.")
    for item in order.items:
        detail = item.title
        if item.variant_title:
            detail += " (" + item.variant_title + ")"
        lines.append("  x" + str(item.quantity) + "  " + detail)

    if not order.is_cancelled:
        # Shopify cancels in a background job. Saying so is more useful than implying
        # the owner can rely on the admin already showing it.
        lines += ["", "Note: Shopify had not finished cancelling when this was sent. "
                      "Check the order in the admin."]

    link = orders_service.admin_url(order)
    if link:
        lines += ["", "Open in Shopify: " + link]
    lines += ["", "Conversation id: " + (conversation_id or "unknown")]

    message.set_content("\n".join(lines))
    try:
        await asyncio.to_thread(_send, message)
    except Exception as exc:  # noqa: BLE001 - a failed email must not break the chat
        logger.exception("Could not email the cancellation of %s: %s", order.number, exc)
        return False

    logger.info("Emailed cancellation of %s to %s", order.number, settings.store_owner_email)
    return True


# --- gathering context ----------------------------------------------------


async def _order_for(ticket: Ticket) -> Optional[Order]:
    """Read the order the ticket names, if it names one.

    Deliberately the staff lookup rather than ``get_order_status``: the recipient is the
    store owner, and a customer who gave their phone in the chat but ordered with their
    email would otherwise produce an email with the order mysteriously absent. Whether
    the contact actually matches is reported in the body instead of hiding the order.
    """
    if not ticket.order_number:
        return None
    try:
        return await orders_service.lookup_for_staff(ticket.order_number)
    except Exception as exc:  # noqa: BLE001 - Shopify being down must not lose the email
        logger.warning("Could not read order %s for %s: %s",
                       ticket.order_number, _what(ticket), exc)
        return None


def _transcript_for(ticket: Ticket) -> List[dict]:
    try:
        return chat_service.transcript(ticket.conversation_id, TRANSCRIPT_LIMIT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read conversation %s for %s: %s",
                       ticket.conversation_id, _what(ticket), exc)
        return []


def _what(subject) -> str:
    """How to name the thing being announced, in a log line.

    Both helpers above serve tickets and feedback. A ticket has a reference the owner
    quotes back; feedback deliberately has none (see ``feedback.schemas``), so there is
    nothing better to call it than what it is.
    """
    reference = getattr(subject, "reference", "")
    return ("ticket " + reference) if reference else "feedback"


# --- the email itself -----------------------------------------------------


def _build(
    ticket: Ticket,
    order: Optional[Order] = None,
    transcript: Optional[List[dict]] = None,
) -> EmailMessage:
    """Everything needed to act on the ticket, without opening the app or Shopify."""
    message = EmailMessage()
    message["Subject"] = _subject(ticket, order)
    message["From"] = settings.smtp_from
    message["To"] = settings.store_owner_email
    if _reply_to(ticket):
        # So hitting reply reaches the customer rather than the robot.
        message["Reply-To"] = _reply_to(ticket)

    lines = [
        "A customer raised something the assistant could not resolve.",
        "",
        "Reference:  " + ticket.reference,
        "Category:   " + ticket.label,
        "Customer:   " + (ticket.customer_name or "not given"),
        "Contact:    " + (ticket.contact or "not given"),
    ]
    if ticket.order_number:
        lines.append("Order:      " + ticket.order_number)
    lines += [
        "Channel:    " + ticket.channel,
        "Logged:     " + (ticket.created_at.strftime("%Y-%m-%d %H:%M UTC")
                          if ticket.created_at else "just now"),
        "",
        "What they said:",
        ticket.summary,
    ]
    lines += _order_section(ticket, order)
    lines += _transcript_section(transcript or [])
    lines += ["", "Conversation id: " + (ticket.conversation_id or "unknown")]

    message.set_content("\n".join(lines))
    return message


def _build_feedback(
    feedback: Feedback,
    order: Optional[Order] = None,
    transcript: Optional[List[dict]] = None,
) -> EmailMessage:
    """An unhappy customer, with enough around it to decide whether to act.

    Shorter than a ticket email on purpose. Feedback is not a job queued for someone -
    nobody is waiting on a reference number - so this leads with what they actually said.
    """
    message = EmailMessage()
    message["Subject"] = "[Feedback] " + feedback.label + _order_suffix(feedback, order)
    message["From"] = settings.smtp_from
    message["To"] = settings.store_owner_email
    if _reply_to(feedback):
        message["Reply-To"] = _reply_to(feedback)

    lines = [
        "A customer left feedback that is not positive.",
        "",
        "Customer:   " + (feedback.customer_name or "not given"),
        "Contact:    " + (feedback.contact or "not given"),
        "Channel:    " + feedback.channel,
        "Left:       " + (feedback.created_at.strftime("%Y-%m-%d %H:%M UTC")
                          if feedback.created_at else "just now"),
        "",
        "What they said:",
        feedback.comment,
    ]
    lines += _order_section(feedback, order)
    lines += _transcript_section(transcript or [])
    lines += ["", "Conversation id: " + (feedback.conversation_id or "unknown")]

    message.set_content("\n".join(lines))
    return message


def _order_suffix(subject, order: Optional[Order]) -> str:
    number = (order.number if order else getattr(subject, "order_number", None)) or ""
    return (" - order " + number) if number else ""


def _subject(ticket: Ticket, order: Optional[Order]) -> str:
    """Reference, what it is about, and the order - all readable in an inbox list."""
    return "[" + ticket.reference + "] " + ticket.label + _order_suffix(ticket, order)


def _order_section(ticket: Ticket, order: Optional[Order]) -> List[str]:
    """The order as it stands right now, so nobody has to go and look it up."""
    if not ticket.order_number:
        return []

    lines = ["", "THE ORDER"]
    if order is None:
        # Said plainly rather than left blank: "no order named #1009" and "Shopify was
        # unreachable" both land here, and either is worth a human knowing.
        lines.append("Could not read " + ticket.order_number +
                     " - it may not exist, or Shopify was unreachable. Check by hand.")
        return lines

    lines += [
        "Number:     " + order.number,
        "Placed:     " + (order.placed_on or "unknown"),
        "Status:     " + order.status_words,
        "Payment:    " + (order.financial_status.lower().replace("_", " ") or "unknown")
        + (" (cash on delivery)" if order.cash_on_delivery else ""),
    ]
    if order.subtotal:
        lines.append("Items:      " + _money(order.subtotal, order.currency))
    if order.delivery:
        lines.append("Delivery:   " + _money(order.delivery, order.currency))
    lines.append("Total:      " + _money(order.total, order.currency))

    # The contact on the order, not the one they typed into the chat - it is what the
    # store actually has to reach them on.
    if order.phone or order.email:
        lines.append("On order:   " + ", ".join(
            part for part in (order.phone, order.email) if part))
    if order.ships_to_city or order.ships_to_country:
        lines.append("Ships to:   " + ", ".join(
            part for part in (order.ships_to_city, order.ships_to_country) if part))

    if order.tracking and not order.tracking.is_empty:
        tracking = " ".join(part for part in (order.tracking.company,
                                              order.tracking.number) if part)
        lines.append("Tracking:   " + (tracking or order.tracking.url or ""))

    for item in order.items:
        detail = item.title
        if item.variant_title:
            detail += " (" + item.variant_title + ")"
        lines.append("  x" + str(item.quantity) + "  " + detail)

    # A ticket quoting an order the customer cannot prove is theirs is worth a second
    # look before anyone refunds anything.
    if ticket.contact and not orders_service.contact_matches(order, ticket.contact):
        lines.append("")
        lines.append("Note: the contact they gave in the chat is not the one on this "
                     "order. Verify who you are talking to before acting.")

    link = orders_service.admin_url(order)
    if link:
        lines += ["", "Open in Shopify: " + link]
    return lines


def _transcript_section(transcript: List[dict]) -> List[str]:
    """What was actually said, as evidence behind the model's one-line summary."""
    if not transcript:
        return []

    lines = ["", "THE CONVERSATION (last " + str(len(transcript)) + " messages)"]
    body: List[str] = []
    for row in transcript:
        who = "Customer" if row.get("role") == "user" else "Assistant"
        content = (row.get("content") or "").strip()
        if content:
            body.append(who + ": " + content)

    joined = "\n".join(body)
    if len(joined) > MAX_TRANSCRIPT_CHARS:
        # Keep the end: the issue is at the end of a conversation, not the greeting.
        joined = "[earlier messages trimmed]\n" + joined[-MAX_TRANSCRIPT_CHARS:]
    lines.append(joined)
    return lines


def _money(amount: str, currency: str) -> str:
    return (amount + " " + currency).strip() or "unknown"


def _reply_to(ticket: Ticket) -> Optional[str]:
    contact = (ticket.contact or "").strip()
    return contact if "@" in contact else None


def _send(message: EmailMessage) -> None:
    """One blocking SMTP delivery. Called in a worker thread."""
    timeout = settings.smtp_timeout_seconds
    if settings.smtp_use_ssl:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
    try:
        if settings.smtp_use_tls and not settings.smtp_use_ssl:
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_pass or "")
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 - the mail is already sent by this point
            pass
