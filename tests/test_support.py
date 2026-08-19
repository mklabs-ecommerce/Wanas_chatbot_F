"""Support tickets, and the email that announces them.

The load-bearing rule here is the ordering: the ticket is stored *before* anyone is
notified, so a mail server being down downgrades the outcome from "someone was emailed"
to "it is written down" rather than losing a customer's complaint entirely.
"""

import pytest

from app.modules.chat import tools
from app.modules.notifications import service as notifications
from app.modules.support import repository, service
from app.modules.support.schemas import CATEGORIES, Ticket
from app.modules.support.service import TicketRejected

# Captured before the autouse fixture below replaces it on the module, so the email tests
# can exercise the real thing while everything else stays offline.
REAL_NOTIFY = notifications.notify_new_ticket


@pytest.fixture(autouse=True)
def quiet_email(monkeypatch):
    """Nothing in the suite ever opens a socket to a mail server."""
    sent = []

    async def fake_notify(ticket):
        sent.append(ticket)
        return True

    monkeypatch.setattr(service.notifications, "notify_new_ticket", fake_notify)
    return sent


ISSUE = dict(
    category="damaged_or_faulty",
    summary="The hoodie arrived with a tear along the left sleeve seam.",
    contact="mona@example.com",
    customer_name="Mona Hassan",
)


# --- logging an issue -----------------------------------------------------


async def test_a_ticket_is_stored_and_given_a_reference(quiet_email):
    ticket = await service.create_ticket(conversation_id="c-1", **ISSUE)

    assert ticket.reference.startswith("WG-")
    assert repository.get(ticket.reference).summary == ISSUE["summary"]
    assert quiet_email == [ticket]


def test_references_avoid_characters_that_are_misheard():
    """A customer reads this down a phone line; O/0 and I/1 cost a support call."""
    for _ in range(50):
        reference = repository.new_reference()
        assert not set(reference[3:]) & set("O0I1SB58Z2")


async def test_the_conversation_and_channel_come_from_the_request(quiet_email):
    """So the store owner can go and read what was actually said."""
    ticket = await service.create_ticket(conversation_id="c-9", channel="web", **ISSUE)

    assert ticket.conversation_id == "c-9"
    assert ticket.channel == "web"


async def test_an_order_number_is_kept_when_there_is_one(quiet_email):
    ticket = await service.create_ticket(order_number="#1006", conversation_id="c-1",
                                         **ISSUE)
    assert repository.get(ticket.reference).order_number == "#1006"


@pytest.mark.parametrize("category", CATEGORIES)
async def test_every_offered_category_is_accepted(quiet_email, category):
    ticket = await service.create_ticket(conversation_id="c-1",
                                         **dict(ISSUE, category=category))
    assert ticket.category == category


async def test_an_unknown_category_is_filed_rather_than_refused(quiet_email):
    """A rejected ticket loses the complaint; "other" is always truthful."""
    ticket = await service.create_ticket(conversation_id="c-1",
                                         **dict(ISSUE, category="spontaneous_combustion"))
    assert ticket.category == "other"


# --- what must not be logged ---------------------------------------------


async def test_a_ticket_without_a_contact_is_refused(quiet_email):
    """Otherwise the store reads a complaint it has no way to answer."""
    with pytest.raises(TicketRejected):
        await service.create_ticket(**dict(ISSUE, contact=""))
    assert repository.count() == 0


@pytest.mark.parametrize("summary", ["", "   ", "broken"])
async def test_a_ticket_without_a_real_description_is_refused(quiet_email, summary):
    with pytest.raises(TicketRejected):
        await service.create_ticket(**dict(ISSUE, summary=summary))
    assert repository.count() == 0


async def test_a_very_long_complaint_is_kept_but_trimmed(quiet_email):
    ticket = await service.create_ticket(conversation_id="c-1",
                                         **dict(ISSUE, summary="x" * 5000))
    assert len(ticket.summary) == service.MAX_SUMMARY_LENGTH


# --- one issue, one ticket ------------------------------------------------


async def test_the_same_issue_restated_does_not_become_a_second_ticket(quiet_email):
    """A customer repeating themselves must not fill the owner's inbox."""
    first = await service.create_ticket(conversation_id="c-1", **ISSUE)
    again = await service.create_ticket(conversation_id="c-1", **ISSUE)

    assert again.reference == first.reference
    assert repository.count() == 1
    # And the owner is emailed once, not twice.
    assert len(quiet_email) == 1


async def test_a_different_issue_in_the_same_conversation_is_its_own_ticket(quiet_email):
    await service.create_ticket(conversation_id="c-1", **ISSUE)
    other = await service.create_ticket(conversation_id="c-1",
                                        **dict(ISSUE, category="delivery_problem"))

    assert repository.count() == 2
    assert other.category == "delivery_problem"


async def test_another_customer_with_the_same_problem_gets_their_own_ticket(quiet_email):
    first = await service.create_ticket(conversation_id="c-1", **ISSUE)
    second = await service.create_ticket(conversation_id="c-2", **ISSUE)

    assert first.reference != second.reference
    assert repository.count() == 2


# --- the email ------------------------------------------------------------


def _ticket():
    return Ticket(reference="WG-K7P3QX", category="damaged_or_faulty",
                  summary="Torn sleeve on arrival.", customer_name="Mona Hassan",
                  contact="mona@example.com", order_number="#1006",
                  conversation_id="c-1", channel="web")


async def test_no_email_configured_still_stores_the_ticket(monkeypatch):
    """The common case while the store is still being set up."""
    monkeypatch.setattr(notifications.settings, "smtp_host", None, raising=False)
    assert await REAL_NOTIFY(_ticket()) is False


async def test_a_mail_failure_never_raises(monkeypatch):
    """A broken mail server must not break the customer's conversation."""
    monkeypatch.setattr(notifications.settings, "smtp_host", "mail.example.com",
                        raising=False)
    monkeypatch.setattr(notifications.settings, "smtp_from", "bot@example.com",
                        raising=False)
    monkeypatch.setattr(notifications.settings, "store_owner_email", "owner@example.com",
                        raising=False)

    def explode(_message):
        raise OSError("connection refused")

    monkeypatch.setattr(notifications, "_send", explode)
    assert await REAL_NOTIFY(_ticket()) is False


async def test_a_failed_email_does_not_lose_the_ticket(monkeypatch):
    """The whole reason notifications are a separate module from support."""
    async def failing_notify(ticket):
        return False

    monkeypatch.setattr(service.notifications, "notify_new_ticket", failing_notify)
    ticket = await service.create_ticket(conversation_id="c-1", **ISSUE)

    assert repository.get(ticket.reference) is not None


def test_the_email_carries_what_is_needed_to_act_on_it(monkeypatch):
    monkeypatch.setattr(notifications.settings, "smtp_from", "bot@example.com",
                        raising=False)
    monkeypatch.setattr(notifications.settings, "store_owner_email", "owner@example.com",
                        raising=False)

    message = notifications._build(_ticket())
    body = message.get_content()

    # The order number is in the subject so the inbox list is triageable.
    assert message["Subject"] == "[WG-K7P3QX] Damaged or faulty item - order #1006"
    assert message["To"] == "owner@example.com"
    # Replying should reach the customer, not the robot.
    assert message["Reply-To"] == "mona@example.com"
    for expected in ("WG-K7P3QX", "Mona Hassan", "mona@example.com", "#1006",
                     "Torn sleeve on arrival.", "c-1"):
        assert expected in body


def test_a_phone_only_customer_gets_no_reply_to_header(monkeypatch):
    """A phone number in Reply-To would make the email unsendable."""
    monkeypatch.setattr(notifications.settings, "smtp_from", "bot@example.com",
                        raising=False)
    monkeypatch.setattr(notifications.settings, "store_owner_email", "owner@example.com",
                        raising=False)

    ticket = _ticket()
    ticket.contact = "01067177129"
    message = notifications._build(ticket)

    assert message["Reply-To"] is None
    assert "01067177129" in message.get_content()


# --- through the tools layer ---------------------------------------------


def _arguments(**overrides):
    arguments = {"category": "delivery_problem",
                 "summary": "The parcel was left with a neighbour who is not in.",
                 "contact": "mona@example.com", "customer_name": "Mona"}
    arguments.update(overrides)
    return arguments


async def test_the_tool_logs_a_ticket_and_returns_the_reference(quiet_email):
    result = await tools.dispatch("create_support_ticket", _arguments(),
                                  context=tools.ToolContext(conversation_id="c-3"))

    assert result["logged"] is True
    assert result["ticket"]["reference"].startswith("WG-")
    assert repository.get(result["ticket"]["reference"]).conversation_id == "c-3"


async def test_the_model_cannot_attach_a_ticket_to_another_conversation(quiet_email):
    """The conversation id is a fact about the request, not the model's to choose."""
    await tools.dispatch("create_support_ticket",
                         _arguments(conversation_id="somebody-elses"),
                         context=tools.ToolContext(conversation_id="c-3"))

    assert repository.get(
        (await tools.dispatch("create_support_ticket", _arguments(category="complaint"),
                              context=tools.ToolContext(conversation_id="c-3"))
         )["ticket"]["reference"]).conversation_id == "c-3"


async def test_a_missing_contact_comes_back_as_data_not_an_exception(quiet_email):
    result = await tools.dispatch("create_support_ticket", _arguments(contact=""),
                                  context=tools.ToolContext(conversation_id="c-3"))

    assert result["logged"] is False
    assert result["error"] == "rejected"


async def test_a_storage_failure_is_reported_rather_than_breaking_the_turn(monkeypatch):
    async def explode(**_kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(tools.support_service, "create_ticket", explode)
    result = await tools.dispatch("create_support_ticket", _arguments(),
                                  context=tools.ToolContext(conversation_id="c-3"))

    assert result["logged"] is False
    assert result["error"] == "ticket_not_saved"


async def test_the_customer_is_not_read_their_own_details_back(quiet_email):
    """The payload carries the reference and nothing they already know."""
    result = await tools.dispatch("create_support_ticket", _arguments(),
                                  context=tools.ToolContext(conversation_id="c-3"))
    flattened = str(result)

    assert "mona@example.com" not in flattened
    assert "neighbour" not in flattened


async def test_ticket_results_carry_data_only_never_instructions(quiet_email):
    results = [
        await tools.dispatch("create_support_ticket", _arguments(),
                             context=tools.ToolContext(conversation_id="c-3")),
        await tools.dispatch("create_support_ticket", _arguments(contact=""),
                             context=tools.ToolContext(conversation_id="c-3")),
    ]
    for result in results:
        flattened = str(result).lower()
        for directive in ("tell the customer", "you should", "ask the customer",
                          "instruction", "apologise"):
            assert directive not in flattened, result


async def test_the_prompt_stops_a_question_becoming_a_complaint():
    from app.modules.chat import agent

    prompt = agent.build_system_prompt()
    assert "Do not log a ticket for something you can answer yourself" in prompt
    assert "Never invent a reference number" in prompt


async def test_the_language_rule_is_repeated_at_the_end_of_the_prompt():
    """Live fault: an English complaint got an Arabic reply.

    LANGUAGE sits at the top of a prompt that has grown past 10k characters, and it was
    lost by the time the model reached a long tool-guidance section. Restating it last
    costs nothing and puts it where recency helps.
    """
    from app.modules.chat import agent

    prompt = agent.build_system_prompt()
    assert "BEFORE YOU SEND, CHECK THE LANGUAGE" in prompt
    # It must genuinely be near the end, not merely present.
    assert prompt.index("BEFORE YOU SEND") > len(prompt) * 0.8


# --- what else the email carries -----------------------------------------
#
# The ticket alone is thin: a reference, a category and the model's own one-line summary
# of a conversation it also conducted. The email pulls in the order as it stands and the
# transcript behind the summary, both defensively - neither may cost the owner the email.


def _order(**overrides):
    from app.modules.orders.schemas import LineItem, Order

    fields = dict(id="gid://shopify/Order/5551234", number="#1006",
                  placed_on="2026-08-17", financial_status="PENDING",
                  fulfillment_status="UNFULFILLED", total="1180", currency="EGP",
                  subtotal="1000", delivery="180", cash_on_delivery=True,
                  email="mona@example.com", phone="+201067177129",
                  ships_to_city="Cairo", ships_to_country="Egypt",
                  items=[LineItem(title="Cairokee T-shirt", quantity=2,
                                  variant_title="L / Black")])
    fields.update(overrides)
    return Order(**fields)


def test_the_email_carries_the_order_as_it_stands(monkeypatch):
    """So nobody has to open Shopify to know what the complaint is about."""
    monkeypatch.setattr(notifications.settings, "shopify_store", "p0hd05-m5.myshopify.com",
                        raising=False)
    body = notifications._build(_ticket(), order=_order()).get_content()

    for expected in ("#1006", "2026-08-17", "not shipped yet", "cash on delivery",
                     "1000 EGP", "180 EGP", "1180 EGP", "Cairo",
                     "x2  Cairokee T-shirt (L / Black)",
                     "+201067177129"):
        assert expected in body, body
    # One click to act on it.
    assert "https://admin.shopify.com/store/p0hd05-m5/orders/5551234" in body


def test_an_unreadable_order_is_said_plainly_rather_than_left_blank():
    """"No such order" and "Shopify was down" both land here, and both matter."""
    body = notifications._build(_ticket(), order=None).get_content()

    assert "Could not read #1006" in body
    assert "Check by hand" in body


def test_a_contact_that_does_not_match_the_order_is_flagged():
    """A refund request quoting someone else's order is worth a second look."""
    ticket = _ticket()
    ticket.contact = "someone.else@example.com"
    body = notifications._build(ticket, order=_order()).get_content()

    assert "not the one on this order" in body


def test_a_matching_contact_is_not_flagged():
    body = notifications._build(_ticket(), order=_order()).get_content()
    assert "not the one on this order" not in body


def test_a_ticket_with_no_order_number_has_no_order_section():
    ticket = _ticket()
    ticket.order_number = None
    body = notifications._build(ticket).get_content()

    assert "THE ORDER" not in body
    assert "order" not in (notifications._subject(ticket, None))


def test_the_email_carries_the_conversation_behind_the_summary():
    """The summary is the model's account of the chat; this is the evidence."""
    transcript = [{"role": "user", "content": "the sleeve is torn"},
                  {"role": "model", "content": "I am sorry to hear that"}]
    body = notifications._build(_ticket(), transcript=transcript).get_content()

    assert "Customer: the sleeve is torn" in body
    assert "Assistant: I am sorry to hear that" in body


def test_a_very_long_conversation_keeps_the_end_not_the_greeting():
    transcript = ([{"role": "user", "content": "x" * 100} for _ in range(60)]
                  + [{"role": "user", "content": "and this is the actual problem"}])
    body = notifications._build(_ticket(), transcript=transcript).get_content()

    assert "and this is the actual problem" in body
    assert "[earlier messages trimmed]" in body


async def test_shopify_being_down_still_sends_the_email(monkeypatch):
    """The whole module's rule, applied to its own enrichment."""
    async def explode(_number):
        raise RuntimeError("shopify is unreachable")

    monkeypatch.setattr(notifications.orders_service, "lookup_for_staff", explode)
    assert await notifications._order_for(_ticket()) is None


def test_a_broken_conversation_read_still_sends_the_email(monkeypatch):
    def explode(_conversation_id, _limit):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(notifications.chat_service, "transcript", explode)
    assert notifications._transcript_for(_ticket()) == []


# --- the staff lookup it relies on ----------------------------------------


async def test_the_staff_lookup_is_not_reachable_by_the_model():
    """It skips the contact check, so it must never be something the model can call."""
    from app.modules.chat import tools

    assert "lookup_for_staff" not in tools.names()
    for declaration in tools.declarations():
        assert "staff" not in declaration["name"]


def test_the_admin_link_is_dropped_rather_than_guessed(monkeypatch):
    from app.modules.orders import service as orders_service
    from app.modules.orders.schemas import Order

    monkeypatch.setattr(orders_service.settings, "shopify_store", "", raising=False)
    assert orders_service.admin_url(_order()) is None
    monkeypatch.setattr(orders_service.settings, "shopify_store", "shop.myshopify.com",
                        raising=False)
    assert orders_service.admin_url(Order(id="", number="#1")) is None
