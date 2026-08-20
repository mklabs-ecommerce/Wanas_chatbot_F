"""Instagram comments: who gets in, what a comment is judged to be, and what happens.

Most of what is asserted here is about restraint. The shop acts in public on this path,
where a mistake is read by everyone and cannot really be taken back, so the tests that
matter are the ones proving nothing is said when we are not sure, nothing is said twice,
and nothing is said because someone forged a request.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.integrations.instagram import client as ig_client
from app.integrations.llm_types import LLMError
from app.main import app
from app.modules.engagement import repository, service
from app.modules.engagement.schemas import (
    IMPORTANT,
    NEGATIVE,
    NEITHER,
    POSITIVE,
    Classification,
    CommentEvent,
    PostProduct,
    parse_webhook,
)

SECRET = "test-app-secret"
OUR_ID = "17841400000000000"


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """A configured, enabled, non-dry store unless a test says otherwise."""
    monkeypatch.setattr(settings, "instagram_app_secret", SECRET, raising=False)
    monkeypatch.setattr(settings, "instagram_access_token", "token", raising=False)
    monkeypatch.setattr(settings, "instagram_business_account_id", OUR_ID, raising=False)
    monkeypatch.setattr(settings, "instagram_webhook_verify_token", "verify-me",
                        raising=False)
    monkeypatch.setattr(settings, "instagram_engagement_enabled", True, raising=False)
    monkeypatch.setattr(settings, "instagram_dry_run", False, raising=False)
    monkeypatch.setattr(settings, "instagram_public_replies", True, raising=False)
    ig_client.reset_pace()
    yield
    ig_client.reset_pace()


class FakeClient:
    """Stands in for Instagram. Records what would have been sent."""

    def __init__(self, **outcomes):
        self.likes = []
        self.public = []
        self.private = []
        self.sent = []
        self.outcomes = outcomes

    @property
    def account_id(self):
        return OUR_ID

    async def like_comment(self, comment_id):
        if "like" in self.outcomes:
            raise self.outcomes["like"]
        self.likes.append(comment_id)
        return True

    async def reply_to_comment(self, comment_id, message):
        if "public" in self.outcomes:
            raise self.outcomes["public"]
        self.public.append((comment_id, message))
        return {"id": "reply-1"}

    async def send_private_reply(self, comment_id, message):
        if "private" in self.outcomes:
            raise self.outcomes["private"]
        self.private.append((comment_id, message))
        return {"message_id": "m1", "recipient_id": "igsid-9"}

    async def send_message(self, recipient_id, text):
        self.sent.append((recipient_id, text))
        return ["m2"]

    async def get_media(self, media_id):
        return self.outcomes.get("media", {"id": media_id, "caption": ""})

    async def download(self, url, max_bytes=None):
        return self.outcomes.get("bytes", b"")


@pytest.fixture
def fake(monkeypatch):
    made = FakeClient()
    monkeypatch.setattr(service, "_client", lambda: made)
    return made


def _comment(text="how much?", comment_id="c1", author="u1", media="m1", username="sara"):
    return CommentEvent(comment_id=comment_id, media_id=media, text=text,
                        author_id=author, username=username)


def _classified(monkeypatch, kind, source="model"):
    async def fake(text):
        return Classification(kind=kind, reason="test", source=source)
    monkeypatch.setattr(service, "classify_comment", fake)


def _no_product(monkeypatch):
    async def fake(media_id):
        return PostProduct(media_id=media_id)
    monkeypatch.setattr(service, "resolve_post_product", fake)


# --- the webhook door -----------------------------------------------------


@pytest.fixture
def client():
    return TestClient(app)


def _signed(body: bytes) -> dict:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": "sha256=" + digest, "Content-Type": "application/json"}


def test_the_subscription_handshake_echoes_the_challenge(client):
    response = client.get("/webhooks/instagram", params={
        "hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert response.status_code == 200
    assert response.text == "42"


def test_a_wrong_verify_token_is_refused(client):
    response = client.get("/webhooks/instagram", params={
        "hub.mode": "subscribe", "hub.verify_token": "guess", "hub.challenge": "42"})
    assert response.status_code == 403


def test_a_forged_delivery_is_refused(client):
    """Without this anyone who found the URL could make the shop reply in public."""
    body = json.dumps({"entry": []}).encode()
    response = client.post("/webhooks/instagram", content=body, headers={
        "X-Hub-Signature-256": "sha256=" + "0" * 64,
        "Content-Type": "application/json",
    })
    assert response.status_code == 403


def test_an_unsigned_delivery_is_refused(client):
    body = json.dumps({"entry": []}).encode()
    response = client.post("/webhooks/instagram", content=body,
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 403


def test_no_app_secret_means_nothing_is_accepted(client, monkeypatch):
    """Fails closed: an unset secret must not quietly become "trust everyone"."""
    body = json.dumps({"entry": []}).encode()
    headers = _signed(body)
    monkeypatch.setattr(settings, "instagram_app_secret", "", raising=False)
    assert client.post("/webhooks/instagram", content=body, headers=headers).status_code == 403


def test_a_genuine_delivery_is_accepted(client):
    body = json.dumps({"object": "instagram", "entry": []}).encode()
    response = client.post("/webhooks/instagram", content=body, headers=_signed(body))
    assert response.status_code == 200


def test_a_body_that_is_not_json_still_answers_200(client):
    """Meta disables a subscription that keeps erroring, and silence is hard to notice."""
    body = b"not json at all"
    response = client.post("/webhooks/instagram", content=body, headers=_signed(body))
    assert response.status_code == 200


# --- reading the payload --------------------------------------------------


COMMENT_PAYLOAD = {
    "object": "instagram",
    "entry": [{
        "id": OUR_ID,
        "time": 1755600000,
        "changes": [{
            "field": "comments",
            "value": {
                "id": "c-100",
                "text": "بكام دي؟",
                "from": {"id": "u-7", "username": "sara"},
                "media": {"id": "media-1"},
            },
        }],
    }],
}


def test_a_comment_payload_becomes_a_comment_event():
    events = parse_webhook(COMMENT_PAYLOAD)
    assert len(events) == 1
    event = events[0]
    assert event.comment_id == "c-100"
    assert event.media_id == "media-1"
    assert event.username == "sara"
    assert event.handle == "@sara"


def test_an_unknown_webhook_field_is_ignored():
    """New fields appear without warning; one we do not handle is not an error."""
    payload = {"entry": [{"changes": [{"field": "mentions", "value": {"id": "x"}}]}]}
    assert parse_webhook(payload) == []


def test_several_events_in_one_delivery_are_all_read():
    payload = {"entry": [
        {"changes": [{"field": "comments", "value": {"id": "a", "media": {"id": "m"}}}]},
        {"changes": [{"field": "comments", "value": {"id": "b", "media": {"id": "m"}}}]},
    ]}
    assert [event.comment_id for event in parse_webhook(payload)] == ["a", "b"]


# --- the claim ------------------------------------------------------------


def test_an_event_can_only_be_claimed_once():
    assert repository.claim("c-1", repository.KIND_COMMENT) is True
    assert repository.claim("c-1", repository.KIND_COMMENT) is False


def test_a_redelivered_comment_is_not_handled_twice(client):
    """Meta redelivers anything it did not get a fast 200 for."""
    body = json.dumps(COMMENT_PAYLOAD).encode()
    headers = _signed(body)

    scheduled = []

    class Recorder:
        def add_task(self, func, *args):
            scheduled.append(args[0].comment_id)

    service.accept(COMMENT_PAYLOAD, Recorder())
    service.accept(COMMENT_PAYLOAD, Recorder())
    assert scheduled == ["c-100"]


def test_our_own_comment_is_ignored():
    """Our public reply arrives back as a comment; answering it is how a bot loops."""
    payload = {"entry": [{"changes": [{"field": "comments", "value": {
        "id": "c-own", "text": "Thanks!", "from": {"id": OUR_ID, "username": "wanas"},
        "media": {"id": "m"}}}]}]}

    scheduled = []

    class Recorder:
        def add_task(self, func, *args):
            scheduled.append(args)

    service.accept(payload, Recorder())
    assert scheduled == []
    assert repository.handled("c-own") is None


def test_nothing_is_claimed_while_instagram_is_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "instagram_engagement_enabled", False, raising=False)

    class Recorder:
        def add_task(self, func, *args):
            raise AssertionError("nothing should be scheduled")

    service.accept(COMMENT_PAYLOAD, Recorder())
    assert repository.handled("c-100") is None


# --- classifying ----------------------------------------------------------


async def test_a_comment_that_only_tags_a_friend_is_neither():
    """The owner's call: tagging a friend is pointing, not asking."""
    result = await service.classify_comment("@sara @mona")
    assert result.kind == NEITHER
    assert result.source == "rule"


async def test_a_row_of_hearts_is_positive_without_spending_a_request():
    result = await service.classify_comment("❤❤❤")
    assert result.kind == POSITIVE
    assert result.source == "rule"


async def test_an_angry_reaction_is_negative_without_spending_a_request():
    result = await service.classify_comment("👎💔")
    assert result.kind == NEGATIVE
    assert result.source == "rule"


async def test_an_empty_comment_is_neither():
    assert (await service.classify_comment("   ")).kind == NEITHER


async def test_words_are_sent_to_the_model(monkeypatch):
    asked = {}

    async def fake_generate(**kwargs):
        asked.update(kwargs)

        class Response:
            text = '{"class": "important", "reason": "asks the price"}'
        return Response()

    monkeypatch.setattr(service.llm, "generate", fake_generate)
    result = await service.classify_comment("عايزة اعرف السعر")
    assert result.kind == IMPORTANT
    assert asked["json_output"] is True
    assert asked["temperature"] == 0.0


async def test_the_classifier_uses_its_own_model_when_one_is_set(monkeypatch):
    """Classifying must not be able to starve replying - each has its own budget."""
    monkeypatch.setattr(settings, "gemini_classifier_model", "gemini-2.5-flash",
                        raising=False)
    seen = {}

    async def fake_generate(**kwargs):
        seen.update(kwargs)

        class Response:
            text = '{"class": "neither"}'
        return Response()

    monkeypatch.setattr(service.llm, "generate", fake_generate)
    await service.classify_comment("something with words")
    assert seen["model"] == "gemini-2.5-flash"


async def test_an_unreachable_classifier_falls_to_neither(monkeypatch):
    """Which means no public action at all. Silence is the safe failure."""
    async def fake_generate(**kwargs):
        raise LLMError("out of quota")

    monkeypatch.setattr(service.llm, "generate", fake_generate)
    result = await service.classify_comment("is this available in large?")
    assert result.kind == NEITHER
    assert result.source == "default"


async def test_unreadable_output_falls_to_neither(monkeypatch):
    async def fake_generate(**kwargs):
        class Response:
            text = "I think this one is positive!"
        return Response()

    monkeypatch.setattr(service.llm, "generate", fake_generate)
    assert (await service.classify_comment("hello there")).kind == NEITHER


async def test_a_class_the_model_invented_is_not_trusted(monkeypatch):
    async def fake_generate(**kwargs):
        class Response:
            text = '{"class": "urgent", "reason": "made up"}'
        return Response()

    monkeypatch.setattr(service.llm, "generate", fake_generate)
    assert (await service.classify_comment("hello there")).kind == NEITHER


# --- acting ---------------------------------------------------------------


async def test_a_positive_comment_is_liked_and_nothing_else(fake, monkeypatch):
    _classified(monkeypatch, POSITIVE)
    await service.handle_comment(_comment(text="جميلة اوي"))
    assert fake.likes == ["c1"]
    assert fake.public == [] and fake.private == []


async def test_a_negative_comment_gets_no_public_action(fake, monkeypatch):
    _classified(monkeypatch, NEGATIVE)

    filed = {}

    async def fake_ticket(**kwargs):
        filed.update(kwargs)

        class Ticket:
            reference = "WG-TEST"
        return Ticket()

    monkeypatch.setattr(service.support_service, "create_ticket", fake_ticket)
    await service.handle_comment(_comment(text="أسوأ خدمة"))

    assert fake.likes == [] and fake.public == [] and fake.private == []
    assert filed["channel"] == "instagram"
    assert filed["contact"] == "instagram:@sara"
    assert "أسوأ خدمة" in filed["summary"]


async def test_a_comment_that_is_neither_is_left_completely_alone(fake, monkeypatch):
    _classified(monkeypatch, NEITHER)
    await service.handle_comment(_comment(text="follow me back"))
    assert fake.likes == [] and fake.public == [] and fake.private == []


async def test_an_important_comment_is_dmed_then_answered_publicly(fake, monkeypatch):
    _no_product(monkeypatch)
    _classified(monkeypatch, IMPORTANT)
    await service.handle_comment(_comment(text="بكام دي؟"))

    assert len(fake.private) == 1
    assert len(fake.public) == 1
    # Arabic comment, Arabic wording.
    assert fake.public[0][1] == service.PUBLIC_REPLY_AR


async def test_an_english_comment_is_answered_in_english(fake, monkeypatch):
    _no_product(monkeypatch)
    _classified(monkeypatch, IMPORTANT)
    await service.handle_comment(_comment(text="How much is this?"))
    assert fake.public[0][1] == service.PUBLIC_REPLY_EN


async def test_no_public_promise_is_made_when_the_dm_failed(monkeypatch):
    """The public reply says "we have sent you a DM". It must be true when it appears."""
    broken = FakeClient(private=ig_client.InstagramUnavailable("timeout"))
    monkeypatch.setattr(service, "_client", lambda: broken)
    _no_product(monkeypatch)
    _classified(monkeypatch, IMPORTANT)

    await service.handle_comment(_comment())
    assert broken.public == []


async def test_a_comment_whose_dm_was_already_spent_is_not_answered_publicly(monkeypatch):
    """Meta gives a comment one private reply for all time; a replay must stay quiet."""
    spent = FakeClient(private=ig_client.InstagramRejected(
        "already replied", code=10, subcode=2534014))
    monkeypatch.setattr(service, "_client", lambda: spent)
    _no_product(monkeypatch)
    _classified(monkeypatch, IMPORTANT)

    action = await service.handle_comment(_comment())
    assert spent.public == []
    assert "already spent" in action


async def test_the_dm_names_the_piece_when_the_post_was_matched(fake, monkeypatch):
    async def resolved(media_id):
        return PostProduct(media_id=media_id, title="RINGER BOXY FIT TSHIRT",
                           source="caption")

    monkeypatch.setattr(service, "resolve_post_product", resolved)
    _classified(monkeypatch, IMPORTANT)
    await service.handle_comment(_comment(text="How much?"))

    assert "RINGER BOXY FIT TSHIRT" in fake.private[0][1]


async def test_the_dm_never_carries_a_price(fake, monkeypatch):
    """The cached match answers which piece. What it costs is always a live lookup."""
    async def resolved(media_id):
        return PostProduct(media_id=media_id, title="Cairokee T-shirt", source="caption")

    monkeypatch.setattr(service, "resolve_post_product", resolved)
    _classified(monkeypatch, IMPORTANT)
    await service.handle_comment(_comment(text="How much?"))

    opener = fake.private[0][1]
    assert not any(character.isdigit() for character in opener)


async def test_the_conversation_remembers_the_comment_that_started_it(fake, monkeypatch):
    from app.modules.chat import service as chat_service

    _no_product(monkeypatch)
    _classified(monkeypatch, IMPORTANT)
    await service.handle_comment(_comment(text="عندكم مقاس لارج؟"))

    thread = repository.thread("igsid-9")
    assert thread is not None
    history = chat_service.transcript(thread["conversation_id"])
    assert history[0]["content"].startswith(service.COMMENT_PREFIX)
    assert "عندكم مقاس لارج؟" in history[0]["content"]
    # ...and the opener we actually sent, so the assistant does not greet them twice.
    assert history[1]["content"] == fake.private[0][1]


async def test_the_thread_is_keyed_on_the_id_their_reply_will_arrive_with(fake, monkeypatch):
    """A comment id and a messaging id are different scopes; the private reply joins them."""
    _no_product(monkeypatch)
    _classified(monkeypatch, IMPORTANT)
    await service.handle_comment(_comment())
    assert repository.thread("igsid-9") is not None


async def test_public_replies_can_be_switched_off_without_stopping_the_dm(fake, monkeypatch):
    monkeypatch.setattr(settings, "instagram_public_replies", False, raising=False)
    _no_product(monkeypatch)
    _classified(monkeypatch, IMPORTANT)
    await service.handle_comment(_comment())
    assert len(fake.private) == 1
    assert fake.public == []


# --- the dry run ----------------------------------------------------------


async def test_a_dry_run_sends_absolutely_nothing(fake, monkeypatch):
    monkeypatch.setattr(settings, "instagram_dry_run", True, raising=False)
    _no_product(monkeypatch)

    for kind in (POSITIVE, IMPORTANT, NEGATIVE):
        _classified(monkeypatch, kind)
        await service.handle_comment(_comment(comment_id="dry-" + kind))

    assert fake.likes == [] and fake.public == [] and fake.private == [] and fake.sent == []


async def test_a_dry_run_still_records_what_it_would_have_done(fake, monkeypatch):
    monkeypatch.setattr(settings, "instagram_dry_run", True, raising=False)
    _classified(monkeypatch, POSITIVE)
    repository.claim("c-dry", repository.KIND_COMMENT)
    await service.handle_comment(_comment(comment_id="c-dry"))

    row = repository.handled("c-dry")
    assert row["classification"] == POSITIVE
    assert "would like" in row["action"]


# --- which product a post is about ----------------------------------------


async def test_a_caption_naming_one_product_resolves_the_post(monkeypatch):
    class Product:
        title = "Cairokee T-shirt"

    async def search(query, limit=5):
        return [Product()]

    monkeypatch.setattr(service.catalog_service, "search_products", search)
    monkeypatch.setattr(service, "_client",
                        lambda: FakeClient(media={"caption": "Cairokee tee, back in stock"}))

    resolved = await service.resolve_post_product("media-x")
    assert resolved.title == "Cairokee T-shirt"
    assert resolved.source == "caption"


async def test_a_caption_that_could_be_two_products_resolves_to_neither(monkeypatch):
    """Half a match is worse than none: it becomes the piece the bot assumes they meant."""
    class Product:
        title = "one"

    async def search(query, limit=5):
        return [Product(), Product()]

    async def no_image(media_id, media, client):
        return PostProduct(media_id=media_id)

    monkeypatch.setattr(service.catalog_service, "search_products", search)
    monkeypatch.setattr(service, "_match_image", no_image)
    monkeypatch.setattr(service, "_client", lambda: FakeClient(media={"caption": "new drop"}))

    assert (await service.resolve_post_product("media-y")).resolved is False


async def test_an_unmatched_post_is_remembered_as_unmatched(monkeypatch):
    """Otherwise every comment on it pays to fail again."""
    calls = []

    async def search(query, limit=5):
        calls.append(query)
        return []

    async def no_image(media_id, media, client):
        return PostProduct(media_id=media_id)

    monkeypatch.setattr(service.catalog_service, "search_products", search)
    monkeypatch.setattr(service, "_match_image", no_image)
    monkeypatch.setattr(service, "_client", lambda: FakeClient(media={"caption": "hello"}))

    await service.resolve_post_product("media-z")
    await service.resolve_post_product("media-z")
    assert len(calls) == 1


async def test_a_post_that_shopify_cannot_answer_for_is_simply_unresolved(monkeypatch):
    async def broken(query, limit=5):
        raise RuntimeError("Shopify is down")

    async def no_image(media_id, media, client):
        return PostProduct(media_id=media_id)

    monkeypatch.setattr(service.catalog_service, "search_products", broken)
    monkeypatch.setattr(service, "_match_image", no_image)
    monkeypatch.setattr(service, "_client", lambda: FakeClient(media={"caption": "hi"}))

    assert (await service.resolve_post_product("media-w")).resolved is False


async def test_an_unconfident_image_match_is_not_taken(monkeypatch):
    """The same earned-confidence rule a customer's photo goes through."""
    class Identification:
        confident = False
        matches = [object()]

    async def identify(images):
        return Identification()

    async def search(query, limit=5):
        return []

    monkeypatch.setattr(service.catalog_service, "search_products", search)
    monkeypatch.setattr(service.catalog_service, "identify_product_from_image", identify)
    monkeypatch.setattr(service, "_client", lambda: FakeClient(
        media={"caption": "", "media_url": "https://cdn/photo.jpg"},
        bytes=b"\xff\xd8\xff\xe0" + b"0" * 200))

    assert (await service.resolve_post_product("media-v")).resolved is False
