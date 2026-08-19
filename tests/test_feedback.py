"""What the feedback module promises.

Two owner decisions (2026-08-19) are load-bearing here and are asserted directly:
customers are never asked for a score, and only unhappy feedback is emailed.
"""

import pytest

from app.modules.feedback import repository, service as feedback_service
from app.modules.feedback.schemas import DEFAULT_SENTIMENT, Feedback
from app.modules.feedback.service import FeedbackRejected
from app.modules.notifications import service as notifications


@pytest.fixture(autouse=True)
def _no_email(monkeypatch):
    """Capture what would have been emailed instead of sending it."""
    sent = []

    async def fake_notify(feedback):
        sent.append(feedback)
        return True

    monkeypatch.setattr(notifications, "notify_new_feedback", fake_notify)
    monkeypatch.setattr(feedback_service.notifications, "notify_new_feedback", fake_notify)
    return sent


@pytest.fixture
def emailed(_no_email):
    return _no_email


# --- recording -------------------------------------------------------------


async def test_feedback_is_stored_in_the_customers_own_words():
    said = "التوصيل كان سريع بس المقاس طلع ضيق شوية"
    feedback = await feedback_service.record_feedback(
        comment=said, sentiment="neutral", conversation_id="c1")

    assert feedback.comment == said
    assert repository.count() == 1


async def test_an_empty_comment_is_refused():
    """A blank row teaches whoever reads these back nothing."""
    with pytest.raises(FeedbackRejected):
        await feedback_service.record_feedback(comment="  ", sentiment="positive")

    assert repository.count() == 0


async def test_one_conversation_is_one_opinion():
    """A customer who says thank you twice has not changed their mind."""
    first = await feedback_service.record_feedback(
        comment="خدمة ممتازة", sentiment="positive", conversation_id="c1")
    second = await feedback_service.record_feedback(
        comment="بجد شكرا", sentiment="positive", conversation_id="c1")

    assert second.comment == first.comment
    assert repository.count() == 1


async def test_a_different_conversation_is_a_different_opinion():
    await feedback_service.record_feedback(
        comment="حلو اوي", sentiment="positive", conversation_id="c1")
    await feedback_service.record_feedback(
        comment="حلو اوي", sentiment="positive", conversation_id="c2")

    assert repository.count() == 2


async def test_the_conversation_and_channel_come_from_the_request():
    feedback = await feedback_service.record_feedback(
        comment="tamam", sentiment="positive", conversation_id="abc", channel="web")

    assert feedback.conversation_id == "abc"
    assert feedback.channel == "web"


# --- the sentiment, and which way it fails ---------------------------------


async def test_an_unknown_sentiment_falls_to_negative(emailed):
    """Fails towards a person. A complaint filed as fine is a complaint lost."""
    feedback = await feedback_service.record_feedback(
        comment="mesh 3agebni", sentiment="furious", conversation_id="c1")

    assert feedback.sentiment == DEFAULT_SENTIMENT == "negative"
    assert len(emailed) == 1


async def test_a_missing_sentiment_falls_to_negative(emailed):
    feedback = await feedback_service.record_feedback(
        comment="whatever", sentiment="", conversation_id="c1")

    assert feedback.sentiment == "negative"
    assert len(emailed) == 1


# --- who gets emailed ------------------------------------------------------


async def test_unhappy_feedback_emails_the_owner(emailed):
    await feedback_service.record_feedback(
        comment="الاوردر وصل متأخر جدا", sentiment="negative", conversation_id="c1")

    assert len(emailed) == 1
    assert emailed[0].sentiment == "negative"


@pytest.mark.parametrize("sentiment", ["positive", "neutral"])
async def test_happy_and_neutral_feedback_is_stored_but_not_emailed(emailed, sentiment):
    """The owner's decision: praise does not fill the inbox."""
    await feedback_service.record_feedback(
        comment="كله تمام", sentiment=sentiment, conversation_id="c1")

    assert repository.count() == 1
    assert emailed == []


async def test_feedback_is_stored_even_when_the_email_fails(monkeypatch):
    """The module's founding rule, inherited from support."""
    async def explode(_feedback):
        raise RuntimeError("mail server is gone")

    monkeypatch.setattr(feedback_service.notifications, "notify_new_feedback", explode)

    with pytest.raises(RuntimeError):
        await feedback_service.record_feedback(
            comment="سيء", sentiment="negative", conversation_id="c1")

    # Stored before anyone was told, so the opinion survives the failure.
    assert repository.count() == 1


# --- what the customer is told back ----------------------------------------


def test_the_customer_is_not_given_a_reference_number():
    """Unlike a ticket, nobody is going to chase this up - a code invites a reply."""
    payload = Feedback(comment="anything", sentiment="positive").to_tool_dict()

    assert payload == {"recorded": True, "sentiment": "positive"}
    assert "reference" not in payload


def test_no_contact_details_are_read_back():
    payload = Feedback(comment="x", sentiment="positive",
                       contact="someone@example.com",
                       customer_name="Mona").to_tool_dict()

    assert "someone@example.com" not in str(payload)
    assert "Mona" not in str(payload)


# --- the tool the model sees -----------------------------------------------


def test_the_tool_never_asks_a_customer_to_score_anything():
    """The owner chose comment-only. A model told to 'rate 1-5' would ask for one."""
    from app.modules.chat import tools

    declaration = next(d for d in tools.declarations() if d["name"] == "record_feedback")
    text = str(declaration).lower()

    for forbidden in ("1-5", "1 to 5", "out of five", "star", "score"):
        assert forbidden not in text or "never" in text, forbidden
    assert "never ask a customer to score or rate" in declaration["description"].lower()


def test_the_tool_is_registered_and_gets_the_turns_context():
    from app.modules.chat import tools

    assert "record_feedback" in tools.names()
    # conversation_id and channel are facts about the request, not model choices.
    assert tools._REGISTRY["record_feedback"]["wants_context"] is True


async def test_a_rejected_comment_comes_back_as_a_bare_code():
    """Tool results carry data, never instructions - the model reads them aloud."""
    from app.modules.chat import tools

    result = await tools.dispatch(
        "record_feedback", {"comment": "", "sentiment": "positive"},
        context=tools.ToolContext(conversation_id="c1"))

    assert result["recorded"] is False
    assert result["error"] == "rejected"


# --- asking only once the goods have actually arrived ----------------------
#
# The trigger is not "an order was placed" - at that moment the customer has nothing to
# have an opinion about. It is "the order reached them", which for cash on delivery means
# the courier collected the money at the door.


def _order(**overrides):
    from app.modules.orders.schemas import LineItem, Order

    fields = dict(id="gid://shopify/Order/555", number="#1007",
                  financial_status="PAID", fulfillment_status="FULFILLED",
                  cash_on_delivery=True, total="698", currency="EGP",
                  items=[LineItem(title="RINGER BOXY FIT TSHIRT", quantity=1,
                                  variant_title="L / Brown")])
    fields.update(overrides)
    return Order(**fields)


def test_a_cod_order_has_reached_the_customer_only_once_it_is_paid():
    """The money changes hands at the door, so PAID cannot precede arrival."""
    assert _order(financial_status="PAID").reached_the_customer is True
    assert _order(financial_status="PENDING").reached_the_customer is False


def test_shipped_is_not_the_same_as_arrived():
    """FULFILLED only means it left the shop."""
    order = _order(financial_status="PENDING", fulfillment_status="FULFILLED")
    assert order.reached_the_customer is False


def test_a_cancelled_order_never_counts_as_arrived():
    order = _order(cancelled_at="2026-08-19T10:00:00Z")
    assert order.reached_the_customer is False


def test_a_prepaid_order_is_not_claimed_to_have_arrived():
    """Prepaid is PAID at checkout, days before delivery - it proves nothing."""
    order = _order(cash_on_delivery=False, financial_status="PAID")
    assert order.reached_the_customer is False


async def test_no_review_is_due_before_the_order_arrives(monkeypatch):
    async def still_pending(_number):
        return _order(financial_status="PENDING")

    monkeypatch.setattr(feedback_service.orders_service, "lookup_for_staff", still_pending)
    feedback_service.expect_review("c1", "#1007")

    assert await feedback_service.review_due("c1") is None


async def test_a_review_becomes_due_once_the_order_is_paid(monkeypatch):
    async def delivered(_number):
        return _order()

    monkeypatch.setattr(feedback_service.orders_service, "lookup_for_staff", delivered)
    feedback_service.expect_review("c1", "#1007")

    order = await feedback_service.review_due("c1")
    assert order is not None and order.number == "#1007"


async def test_no_review_is_due_for_a_conversation_that_ordered_nothing():
    assert await feedback_service.review_due("never-ordered") is None


async def test_shopify_being_down_costs_the_prompt_not_the_conversation(monkeypatch):
    async def explode(_number):
        raise RuntimeError("shopify is unreachable")

    monkeypatch.setattr(feedback_service.orders_service, "lookup_for_staff", explode)
    feedback_service.expect_review("c1", "#1007")

    assert await feedback_service.review_due("c1") is None


async def test_answering_stops_the_bot_asking_again(monkeypatch):
    async def delivered(_number):
        return _order()

    monkeypatch.setattr(feedback_service.orders_service, "lookup_for_staff", delivered)
    feedback_service.expect_review("c1", "#1007")
    assert await feedback_service.review_due("c1") is not None

    await feedback_service.record_feedback(
        comment="المقاس ظبط والخامة حلوة", sentiment="positive",
        order_number="#1007", conversation_id="c1")

    assert await feedback_service.review_due("c1") is None


async def test_declining_stops_the_bot_asking_again(monkeypatch):
    async def delivered(_number):
        return _order()

    monkeypatch.setattr(feedback_service.orders_service, "lookup_for_staff", delivered)
    feedback_service.expect_review("c1", "#1007")

    feedback_service.close_review("c1")
    assert await feedback_service.review_due("c1") is None


def test_the_same_order_is_only_expected_once():
    feedback_service.expect_review("c1", "#1007")
    feedback_service.expect_review("c1", "#1007")

    from app.modules.feedback import repository as repo
    assert repo.open_review("c1") == "#1007"


# --- what the model is told when it is due ---------------------------------


def test_the_prompt_names_the_actual_pieces_when_an_order_has_arrived():
    """"How was your order?" gets nothing. "How was the Ringer in Brown?" gets an answer."""
    from app.modules.chat import agent

    prompt = agent.build_system_prompt(_order())

    assert "THIS CUSTOMER'S ORDER HAS ARRIVED" in prompt
    assert "#1007" in prompt
    assert "RINGER BOXY FIT TSHIRT (L / Brown)" in prompt


def test_the_prompt_says_nothing_about_feedback_when_nothing_has_arrived():
    from app.modules.chat import agent

    prompt = agent.build_system_prompt()

    assert "THIS CUSTOMER'S ORDER HAS ARRIVED" not in prompt


def test_the_bot_is_told_never_to_ask_for_feedback_on_its_own():
    from app.modules.chat import agent

    prompt = agent.build_system_prompt()

    assert "Never ask for feedback on your own initiative" in prompt
    assert "Placing an order is not the moment to ask" in prompt
