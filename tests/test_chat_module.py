"""The chat module: conversation persistence and the agent turn loop."""

import pytest

from app.integrations.llm_types import LLMAllProvidersFailed, LLMResponse
from app.modules.chat import agent, repository, tools


# --- repository (chat's own tables only) ---------------------------------


def test_new_conversation_gets_an_id():
    conversation_id = repository.ensure_conversation(None)
    assert conversation_id
    assert repository.count_messages(conversation_id) == 0


def test_existing_conversation_is_reused():
    first = repository.ensure_conversation(None)
    again = repository.ensure_conversation(first)
    assert again == first


def test_unknown_id_is_recreated_rather_than_rejected():
    """A stale id in a browser tab must not break the widget."""
    recreated = repository.ensure_conversation("id-that-was-never-stored")
    assert recreated == "id-that-was-never-stored"


def test_history_comes_back_oldest_first():
    conversation_id = repository.ensure_conversation(None)
    repository.add_message(conversation_id, repository.ROLE_USER, "first")
    repository.add_message(conversation_id, repository.ROLE_MODEL, "second", model="m")
    repository.add_message(conversation_id, repository.ROLE_USER, "third")

    history = repository.get_recent_messages(conversation_id, limit=10)
    assert [row["content"] for row in history] == ["first", "second", "third"]
    assert [row["role"] for row in history] == ["user", "model", "user"]


def test_history_limit_keeps_the_most_recent_messages():
    conversation_id = repository.ensure_conversation(None)
    for index in range(8):
        repository.add_message(conversation_id, repository.ROLE_USER, "m" + str(index))

    history = repository.get_recent_messages(conversation_id, limit=3)
    # The newest three, still in chronological order.
    assert [row["content"] for row in history] == ["m5", "m6", "m7"]


def test_conversations_are_isolated_from_each_other():
    first = repository.ensure_conversation(None)
    second = repository.ensure_conversation(None)
    repository.add_message(first, repository.ROLE_USER, "in first")

    assert repository.count_messages(first) == 1
    assert repository.count_messages(second) == 0
    assert repository.get_recent_messages(second, limit=10) == []


# --- agent ---------------------------------------------------------------


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace the LLM with a recorder, so the agent is tested without the network."""
    calls = {}

    async def generate(*, turns, system_instruction=None, tools=None, temperature=None,
                       json_output=False, model=None):
        calls["turns"] = list(turns)
        calls["system_instruction"] = system_instruction
        calls["tools"] = tools
        return LLMResponse(text="a reply", model="test-model", provider="test")

    monkeypatch.setattr(agent.llm, "generate", generate)
    return calls


async def test_handle_message_persists_both_sides_of_the_turn(fake_llm):
    reply = await agent.handle_message("hello there")

    assert reply.text == "a reply"
    assert reply.model == "test-model"
    assert reply.degraded is False

    history = repository.get_recent_messages(reply.conversation_id, limit=10)
    assert [(row["role"], row["content"]) for row in history] == [
        ("user", "hello there"),
        ("model", "a reply"),
    ]


async def test_previous_turns_are_replayed_to_the_model(fake_llm):
    first = await agent.handle_message("first question")
    await agent.handle_message("second question", conversation_id=first.conversation_id)

    replayed = [(turn.role, turn.text) for turn in fake_llm["turns"]]
    assert replayed == [
        ("user", "first question"),
        ("model", "a reply"),
        ("user", "second question"),
    ]


async def test_every_registered_tool_is_offered_to_the_model(fake_llm):
    """Superseded step 2's "no tools yet" check once the catalog was wired in."""
    await agent.handle_message("hello")
    assert [tool["name"] for tool in fake_llm["tools"]] == tools.names()


async def test_system_prompt_carries_the_language_and_honesty_rules(fake_llm):
    await agent.handle_message("hello")
    prompt = fake_llm["system_instruction"]

    # Owner's call, reversing the original MSA rule: the shop talks the way its
    # customers do, so Arabic replies are Egyptian colloquial.
    assert "everyday Egyptian Arabic" in prompt
    assert "Never reply in Modern Standard Arabic" in prompt
    assert "Never invent a product" in prompt
    # Cancelling and online payment are built now; changing what is IN an order is not.
    assert "cannot change what is in an order" in prompt
    assert "Never ask for a card number" in prompt


async def test_total_provider_failure_returns_a_polite_reply_not_an_error(monkeypatch):
    async def always_fails(**_kwargs):
        raise LLMAllProvidersFailed("429 everywhere")

    monkeypatch.setattr(agent.llm, "generate", always_fails)
    reply = await agent.handle_message("hello")

    assert reply.text == agent.UNAVAILABLE_REPLY
    assert reply.degraded is True
    assert reply.provider == "none"
    # The customer's message is kept, but no failed model turn is stored, so a retry
    # continues the conversation cleanly instead of replaying an apology.
    history = repository.get_recent_messages(reply.conversation_id, limit=10)
    assert [row["role"] for row in history] == ["user"]


# --- images (step 5) -----------------------------------------------------


def _jpeg():
    from app.integrations.llm_types import ImagePart
    return ImagePart(data=b"\xff\xd8\xff\xe0" + b"\x00" * 200, mime_type="image/jpeg")


async def test_an_image_rides_on_the_current_turn(fake_llm):
    await agent.handle_message("what is this?", images=[_jpeg()])

    turn = fake_llm["turns"][-1]
    assert turn.text == "what is this?"
    assert [image.mime_type for image in turn.images] == ["image/jpeg"]


async def test_history_records_that_a_photo_was_sent(fake_llm):
    """The bytes are not stored, but an empty row would read as if nothing was said."""
    reply = await agent.handle_message("", images=[_jpeg()])
    history = repository.get_recent_messages(reply.conversation_id, limit=10)

    assert history[0]["content"] == "[image]"


async def test_a_caption_is_kept_alongside_the_image_marker(fake_llm):
    reply = await agent.handle_message("is this yours?", images=[_jpeg()])
    history = repository.get_recent_messages(reply.conversation_id, limit=10)

    assert history[0]["content"] == "[image] is this yours?"


async def test_several_images_are_counted_in_history(fake_llm):
    reply = await agent.handle_message("", images=[_jpeg(), _jpeg()])
    history = repository.get_recent_messages(reply.conversation_id, limit=10)

    assert history[0]["content"] == "[2 images]"


async def test_an_earlier_photo_is_not_replayed_on_a_later_turn(fake_llm):
    """The bytes are gone, so the model must not appear to still be looking at them."""
    first = await agent.handle_message("what is this?", images=[_jpeg()])
    await agent.handle_message("and the price?", conversation_id=first.conversation_id)

    assert all(not turn.images for turn in fake_llm["turns"])
    # It can still see that a photo was sent, so it knows to ask for it again.
    assert fake_llm["turns"][0].text == "[image] what is this?"


async def test_the_prompt_tells_the_model_it_cannot_see_earlier_photos(fake_llm):
    prompt = agent.build_system_prompt()
    assert "only in the message it was attached to" in prompt
    assert "ask them to send it again" in prompt


async def test_the_prompt_separates_a_verified_match_from_an_unsettled_one(fake_llm):
    """Superseded step 5's blanket "never identify" rule.

    Step 5's live finding was that the bot searched on its own description of a photo and
    announced "That is our Ringer Boxy Fit T-shirt" - an identification nothing had
    checked. Step 6 replaces the ban with a verified signal: it may assert only what
    identify_product_from_image marked confident, and must ask when it did not.
    """
    prompt = agent.build_system_prompt()
    assert "confident true" in prompt and "confident false" in prompt
    assert "never use search_products to work out what a picture shows" in prompt
    # The unsettled case must be an actual question, not a softened assertion.
    assert "ask which one is theirs" in prompt


# --- what the bot may suggest depends on the live store ------------------


async def test_the_prompt_forbids_sending_customers_to_a_locked_storefront(monkeypatch):
    """Live fault: asked to place an order, the bot replied "you can complete your order
    directly through our online store" - a page the customer cannot open, because the
    storefront is password-protected."""
    from app.modules.catalog import service as catalog_service

    monkeypatch.setattr(catalog_service, "storefront_is_open", lambda: False)
    prompt = agent.build_system_prompt()

    assert "never send them there to order" in prompt
    # Ordering still works here - only the website is shut.
    assert "cash on delivery is placed here, not" in prompt


async def test_that_warning_disappears_once_the_store_is_published(monkeypatch):
    from app.modules.catalog import service as catalog_service

    monkeypatch.setattr(catalog_service, "storefront_is_open", lambda: True)
    prompt = agent.build_system_prompt()

    assert "never send them there to order" not in prompt
    # The build-step limitations are unaffected by the storefront being open.
    assert "cannot change what is in an order" in prompt
