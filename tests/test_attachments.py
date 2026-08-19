"""Image uploads: decoding, sniffing and the guards around both.

The uploader is a stranger, so these tests are mostly about what must be refused. Real
image headers are used - the sniffer reads the first bytes, so a correct header with
padding behind it is indistinguishable from a real photo for its purposes.
"""

import base64

import pytest
from fastapi.testclient import TestClient

from app.modules.chat import agent, attachments
from app.modules.chat.attachments import AttachmentError, decode_images, sniff_mime_type
from app.modules.chat.schemas import ImageUpload

# Real magic numbers for each format the bot accepts.
HEADERS = {
    "image/jpeg": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01",
    "image/png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
    "image/gif": b"GIF89a\x01\x00\x01\x00\x80\x00",
    "image/webp": b"RIFF\x24\x00\x00\x00WEBPVP8 ",
    # An iPhone photo: ISO base media container with a HEIC brand.
    "image/heic": b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00",
}


def _image(mime="image/jpeg", size=400):
    """Valid header for ``mime``, padded out to a plausible file size."""
    header = HEADERS[mime]
    return header + b"\x00" * max(0, size - len(header))


def _upload(data: bytes, mime=None, as_data_url=False):
    encoded = base64.b64encode(data).decode()
    if as_data_url:
        encoded = "data:" + (mime or "image/jpeg") + ";base64," + encoded
    return ImageUpload(data=encoded, mime_type=mime)


# --- sniffing -------------------------------------------------------------


@pytest.mark.parametrize("mime", list(HEADERS))
def test_every_supported_format_is_recognised(mime):
    assert sniff_mime_type(_image(mime)) == mime


@pytest.mark.parametrize("data", [
    b"",
    b"short",
    b"%PDF-1.7\n%\xe2\xe3\xcf\xd3" + b"\x00" * 200,      # a PDF
    b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200,       # a Windows executable
    b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",   # SVG: markup, not a raster image
    b"just some text pretending to be a photo" * 5,
])
def test_anything_that_is_not_a_usable_image_is_refused(data):
    assert sniff_mime_type(data) is None


def test_a_lie_about_the_content_type_is_ignored():
    """Browsers mislabel files and callers can simply lie; the bytes decide."""
    parts = decode_images([_upload(_image("image/png"), mime="image/jpeg")])
    assert parts[0].mime_type == "image/png"


def test_an_executable_labelled_as_a_photo_is_rejected():
    with pytest.raises(AttachmentError):
        decode_images([_upload(b"MZ\x90\x00" + b"\x00" * 300, mime="image/jpeg")])


# --- decoding -------------------------------------------------------------


def test_a_browser_data_url_is_accepted():
    """FileReader hands the widget "data:image/jpeg;base64,..." - the exact shape sent."""
    parts = decode_images([_upload(_image(), as_data_url=True)])
    assert parts[0].mime_type == "image/jpeg"
    assert parts[0].data.startswith(b"\xff\xd8\xff")


def test_bare_base64_is_accepted_too():
    """A channel adapter will hand over bytes, not a browser data URL."""
    assert len(decode_images([_upload(_image())])) == 1


def test_base64_split_across_lines_still_decodes():
    encoded = "\n".join(base64.b64encode(_image()).decode()[i:i + 76]
                        for i in range(0, 600, 76))
    assert len(decode_images([ImageUpload(data=encoded)])) == 1


def test_nothing_attached_is_not_an_error():
    assert decode_images([]) == []
    assert decode_images(None) == []


@pytest.mark.parametrize("bad", ["not base64 at all!!", "///", "===="])
def test_undecodable_data_is_reported_not_crashed(bad):
    with pytest.raises(AttachmentError):
        decode_images([ImageUpload(data=bad)])


# --- limits ---------------------------------------------------------------


def test_too_many_images_are_refused():
    uploads = [_upload(_image()) for _ in range(attachments.MAX_IMAGES + 1)]
    with pytest.raises(AttachmentError, match="at most"):
        decode_images(uploads)


def test_the_maximum_number_is_allowed():
    assert len(decode_images([_upload(_image()) for _ in range(attachments.MAX_IMAGES)])) \
        == attachments.MAX_IMAGES


def test_an_oversized_image_is_refused():
    huge = _image(size=attachments.MAX_IMAGE_BYTES + 1024)
    with pytest.raises(AttachmentError, match="larger than"):
        decode_images([_upload(huge)])


def test_an_oversized_payload_is_rejected_before_it_is_decoded(monkeypatch):
    """Otherwise a hostile caller makes the server expand the payload just to discard it."""
    decoded = []
    real = base64.b64decode
    monkeypatch.setattr(base64, "b64decode",
                        lambda *a, **k: (decoded.append(1), real(*a, **k))[1])

    with pytest.raises(AttachmentError):
        decode_images([ImageUpload(data="A" * (attachments.MAX_IMAGE_BYTES * 2))])
    assert decoded == []


def test_several_images_cannot_add_up_past_the_total_limit():
    each = attachments.MAX_IMAGE_BYTES - 1024
    uploads = [_upload(_image(size=each)) for _ in range(3)]
    with pytest.raises(AttachmentError, match="too large in total"):
        decode_images(uploads)


def test_a_file_too_small_to_hold_a_picture_is_refused():
    with pytest.raises(AttachmentError, match="empty"):
        decode_images([_upload(b"\xff\xd8\xff")])


def test_rejection_messages_are_safe_to_show_a_customer():
    """The route passes these straight through to the widget."""
    for uploads in ([_upload(_image()) for _ in range(9)],
                    [ImageUpload(data="nonsense!")],
                    [_upload(b"MZ\x90\x00" + b"\x00" * 300)]):
        with pytest.raises(AttachmentError) as raised:
            decode_images(uploads)
        message = str(raised.value)
        assert message.endswith((".", "!"))
        for leak in ("Traceback", "base64", "mime", "None", "Error"):
            assert leak not in message


# --- through the endpoint -------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    """The real app, with the LLM replaced by a recorder."""
    from app.integrations.llm_types import LLMResponse
    from app.main import app

    seen = {}

    async def generate(*, turns, system_instruction=None, tools=None, temperature=None,
                       json_output=False, model=None):
        seen["turns"] = list(turns)
        return LLMResponse(text="I can see it.", model="test-model", provider="test")

    monkeypatch.setattr(agent.llm, "generate", generate)
    monkeypatch.setattr(agent.settings, "gemini_api_key", "test-key", raising=False)

    with TestClient(app) as test_client:
        test_client.seen = seen
        yield test_client


def _post(client, **body):
    return client.post("/chat", json=body)


def test_an_image_reaches_the_model(client):
    response = _post(client, message="what is this?",
                     images=[{"data": base64.b64encode(_image()).decode()}])

    assert response.status_code == 200
    turn = client.seen["turns"][-1]
    assert len(turn.images) == 1
    assert turn.images[0].mime_type == "image/jpeg"


def test_a_photo_with_no_caption_is_a_complete_message(client):
    """Customers routinely send a picture and type nothing."""
    response = _post(client, images=[{"data": base64.b64encode(_image()).decode()}])
    assert response.status_code == 200


def test_a_turn_with_neither_text_nor_image_is_refused(client):
    response = _post(client, message="   ")
    assert response.status_code == 422


def test_a_rejected_upload_explains_itself_to_the_customer(client):
    response = _post(client, message="here",
                     images=[{"data": base64.b64encode(b"MZ\x90\x00" + b"\x00" * 300).decode()}])

    assert response.status_code == 422
    assert "supported image" in response.json()["detail"]


def test_a_rejected_upload_is_not_recorded_as_a_conversation_turn(client):
    """The turn never happened, so a retry must not replay a phantom message."""
    from app.modules.chat import repository

    started = _post(client, message="hello").json()["conversation_id"]
    _post(client, message="here", conversation_id=started,
          images=[{"data": "not base64!"}])

    assert repository.count_messages(started) == 2
