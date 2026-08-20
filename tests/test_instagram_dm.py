"""Instagram direct messages - the same assistant, a different transport.

The point of these tests is that nothing about the conversation is re-implemented here.
A DM goes through ``chat.service.handle_message()``, so the catalog, the orders, the
photo matching and the voice-note rules all apply unchanged; what is asserted is the
transport around them, and the two loops it could fall into.
"""

import base64
import math
import struct

import pytest

from app.core.config import settings
from app.integrations.instagram import client as ig_client
from app.modules.chat import service as chat_service
from app.modules.chat.schemas import Answer
from app.modules.engagement import repository, service
from app.modules.engagement.schemas import Attachment, MessageEvent, parse_webhook

OUR_ID = "17841400000000000"
THEM = "igsid-77"


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "instagram_app_secret", "secret", raising=False)
    monkeypatch.setattr(settings, "instagram_access_token", "token", raising=False)
    monkeypatch.setattr(settings, "instagram_business_account_id", OUR_ID, raising=False)
    monkeypatch.setattr(settings, "instagram_engagement_enabled", True, raising=False)
    monkeypatch.setattr(settings, "instagram_dry_run", False, raising=False)
    ig_client.reset_pace()
    yield
    ig_client.reset_pace()


class FakeClient:
    def __init__(self, files=None, send_error=None):
        self.sent = []
        self.files = files or {}
        self.send_error = send_error

    async def send_message(self, recipient_id, text):
        if self.send_error:
            raise self.send_error
        self.sent.append((recipient_id, text))
        return ["mid-1"]

    async def download(self, url, max_bytes=None):
        return self.files[url]


@pytest.fixture
def fake(monkeypatch):
    made = FakeClient()
    monkeypatch.setattr(service, "_client", lambda: made)
    return made


@pytest.fixture
def answered(monkeypatch):
    """Replace the assistant with something that records what it was handed."""
    seen = {}

    async def fake_handle(text="", raw_attachments=(), conversation_id=None, channel="web"):
        seen["text"] = text
        seen["attachments"] = list(raw_attachments)
        seen["conversation_id"] = conversation_id
        seen["channel"] = channel
        return Answer(conversation_id=conversation_id or "conv-1", text="أهلاً بيك")

    monkeypatch.setattr(service.chat_service, "handle_message", fake_handle)
    return seen


def _message(text="عايز اطلب", message_id="mid-a", attachments=None):
    return MessageEvent(message_id=message_id, sender_id=THEM, text=text,
                        attachments=attachments or [])


# --- the two loops --------------------------------------------------------


def test_our_own_echo_is_ignored():
    """Meta echoes what we send on the same webhook; answering it is a loop."""
    payload = {"entry": [{"messaging": [{
        "sender": {"id": OUR_ID}, "recipient": {"id": THEM},
        "message": {"mid": "echo-1", "text": "hi", "is_echo": True}}]}]}

    scheduled = []

    class Recorder:
        def add_task(self, func, *args):
            scheduled.append(args)

    service.accept(payload, Recorder())
    assert scheduled == []
    assert repository.handled("echo-1") is None


def test_a_read_receipt_is_not_a_message():
    payload = {"entry": [{"messaging": [{
        "sender": {"id": THEM}, "read": {"watermark": 1755600000}}]}]}
    assert parse_webhook(payload) == []


def test_a_redelivered_message_is_answered_once():
    payload = {"entry": [{"messaging": [{
        "sender": {"id": THEM}, "message": {"mid": "mid-x", "text": "hello"}}]}]}

    scheduled = []

    class Recorder:
        def add_task(self, func, *args):
            scheduled.append(args[0].message_id)

    service.accept(payload, Recorder())
    service.accept(payload, Recorder())
    assert scheduled == ["mid-x"]


# --- answering ------------------------------------------------------------


async def test_a_message_is_answered_by_the_ordinary_assistant(fake, answered):
    await service.handle_direct_message(_message("بكام التيشيرت البني؟"))

    assert answered["text"] == "بكام التيشيرت البني؟"
    assert answered["channel"] == "instagram"
    assert fake.sent == [(THEM, "أهلاً بيك")]


async def test_a_returning_customer_keeps_their_conversation(fake, answered):
    repository.link_thread(THEM, "conv-earlier")
    await service.handle_direct_message(_message())
    assert answered["conversation_id"] == "conv-earlier"


async def test_a_new_customer_is_linked_to_the_conversation_they_get(fake, answered):
    await service.handle_direct_message(_message())
    thread = repository.thread(THEM)
    assert thread["conversation_id"] == "conv-1"


async def test_the_channel_reaches_the_assistant_so_an_order_is_tagged(fake, answered):
    """Channel attribution is the whole reason an order can be traced back to here."""
    await service.handle_direct_message(_message())
    assert answered["channel"] == service.CHANNEL == "instagram"


async def test_a_message_with_nothing_readable_is_left_alone(fake, answered):
    """A sticker or a story reaction. Nothing was said, so nothing is answered."""
    repository.claim("mid-sticker", repository.KIND_MESSAGE)
    await service.handle_direct_message(_message(text="", message_id="mid-sticker"))
    assert fake.sent == []
    assert repository.handled("mid-sticker")["outcome"] == repository.OUTCOME_SKIPPED


async def test_a_dry_run_answers_nobody(fake, answered, monkeypatch):
    monkeypatch.setattr(settings, "instagram_dry_run", True, raising=False)
    await service.handle_direct_message(_message())
    assert fake.sent == []


async def test_a_failed_delivery_is_not_retried(monkeypatch, answered):
    """The turn is already in the history; resending would double it."""
    broken = FakeClient(send_error=ig_client.InstagramUnavailable("timeout"))
    monkeypatch.setattr(service, "_client", lambda: broken)
    repository.claim("mid-fail", repository.KIND_MESSAGE)

    result = await service.handle_direct_message(_message(message_id="mid-fail"))
    assert result == "delivery failed"
    assert repository.handled("mid-fail")["outcome"] == repository.OUTCOME_FAILED


async def test_an_assistant_failure_does_not_take_the_process_down(fake, monkeypatch):
    async def broken(**kwargs):
        raise RuntimeError("everything is on fire")

    monkeypatch.setattr(service.chat_service, "handle_message", broken)
    repository.claim("mid-boom", repository.KIND_MESSAGE)
    assert await service.handle_direct_message(_message(message_id="mid-boom")) == "failed"
    assert fake.sent == []


# --- attachments ----------------------------------------------------------


def _jpeg() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00" * 300


def _wav(amplitude: int = 8000, seconds: float = 1.0, rate: int = 16000) -> bytes:
    frames = int(rate * seconds)
    samples = [int(amplitude * math.sin(index / 8)) for index in range(frames)]
    data = struct.pack("<" + str(frames) + "h", *samples)
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


async def test_a_photo_reaches_the_assistant(monkeypatch, answered):
    client = FakeClient(files={"https://cdn/photo.jpg": _jpeg()})
    monkeypatch.setattr(service, "_client", lambda: client)

    await service.handle_direct_message(_message(
        text="هي دي؟", attachments=[Attachment(kind="image", url="https://cdn/photo.jpg")]))
    assert len(answered["attachments"]) == 1


async def test_an_attachment_that_cannot_be_downloaded_is_simply_absent(monkeypatch,
                                                                       answered):
    class Broken(FakeClient):
        async def download(self, url, max_bytes=None):
            raise ig_client.InstagramUnavailable("gone")

    monkeypatch.setattr(service, "_client", lambda: Broken())
    await service.handle_direct_message(_message(
        text="look", attachments=[Attachment(kind="image", url="https://cdn/x.jpg")]))
    assert answered["attachments"] == []
    assert answered["text"] == "look"


async def test_a_declared_type_is_not_what_decides_the_kind(monkeypatch):
    """Meta's label is advisory here for the same reason a browser's is."""
    client = FakeClient(files={"https://cdn/mislabelled": _wav()})
    monkeypatch.setattr(service, "_client", lambda: client)

    seen = {}

    async def fake_handle(text="", raw_attachments=(), conversation_id=None, channel="web"):
        images, audio = chat_service._sort_attachments(raw_attachments)
        seen["images"], seen["audio"] = images, audio
        return Answer(conversation_id="c", text="ok")

    monkeypatch.setattr(service.chat_service, "handle_message", fake_handle)
    await service.handle_direct_message(_message(
        attachments=[Attachment(kind="image", url="https://cdn/mislabelled")]))

    assert seen["images"] == [] and len(seen["audio"]) == 1


async def test_a_silent_voice_note_is_refused_here_too(monkeypatch):
    """The gate exists because a model asked to transcribe silence invents a sentence."""
    silent = _wav(amplitude=0)
    client = FakeClient(files={"https://cdn/quiet.wav": silent})
    monkeypatch.setattr(service, "_client", lambda: client)

    await service.handle_direct_message(_message(
        text="", attachments=[Attachment(kind="audio", url="https://cdn/quiet.wav")]))

    assert len(client.sent) == 1
    assert "silent" in client.sent[0][1]


async def test_a_file_the_assistant_cannot_read_is_explained_not_ignored(monkeypatch):
    client = FakeClient(files={"https://cdn/clip.mp4": b"\x00\x00\x00\x18ftypmp42" + b"0" * 100})
    monkeypatch.setattr(service, "_client", lambda: client)

    await service.handle_direct_message(_message(
        text="", attachments=[Attachment(kind="video", url="https://cdn/clip.mp4")]))

    assert len(client.sent) == 1
    assert "photos and voice notes" in client.sent[0][1]


# --- long replies ---------------------------------------------------------


def test_a_short_reply_is_one_message():
    assert ig_client.split_message("hello") == ["hello"]


def test_a_long_reply_is_split_rather_than_truncated():
    """Instagram truncates past its limit, and a cut price or phone number is worse."""
    text = ". ".join("sentence number " + str(index) for index in range(200))
    pieces = ig_client.split_message(text, limit=200)
    assert len(pieces) > 1
    assert all(len(piece) <= 200 for piece in pieces)
    assert "".join(piece.replace(" ", "") for piece in pieces).startswith("sentencenumber0")


def test_splitting_prefers_a_sentence_boundary():
    text = "First sentence here. " + "x" * 40
    pieces = ig_client.split_message(text, limit=30)
    assert pieces[0] == "First sentence here."


def test_an_unbroken_run_is_still_split():
    pieces = ig_client.split_message("x" * 100, limit=30)
    assert len(pieces) == 4
    assert all(len(piece) <= 30 for piece in pieces)


def test_an_empty_reply_sends_nothing():
    assert ig_client.split_message("   ") == []


# --- the door itself ------------------------------------------------------


async def test_the_public_door_is_what_engagement_uses():
    """`chat/service.py` is the one way in; reaching into agent.py would be the bug."""
    import inspect

    source = inspect.getsource(service)
    assert "chat.agent" not in source
    assert "chat_service.handle_message" in source


def test_bytes_go_through_the_same_validation_as_a_browser_upload():
    from app.modules.chat import attachments

    wrapped = attachments.from_bytes(_jpeg(), "image/jpeg")
    assert base64.b64decode(wrapped.data) == _jpeg()
    assert attachments.decode_images([wrapped])[0].mime_type == "image/jpeg"
