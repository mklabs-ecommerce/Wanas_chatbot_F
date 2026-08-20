"""Turning what a browser uploads into bytes the model can be shown or played.

Images and voice notes arrive base64-encoded inside the JSON body rather than as a
multipart upload.
That keeps one request shape for every channel - a WhatsApp adapter will hand over
downloaded bytes, not a browser file part - and keeps the endpoint testable with a plain
JSON client.

Everything here is a guard. The uploader is a stranger, so the declared content type is
not believed: the bytes are sniffed, and anything that is not a real image or recording
Gemini accepts is rejected before it reaches the model.

Sniffing matters more here than it looks. Measured 2026-08-19, Gemini works out for
itself what audio bytes are and ignores the type we send it - so the label is no
protection at all, and this file is the only thing standing between a stranger's upload
and the model.
"""

import base64
import binascii
import logging
import re
import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.integrations.llm_types import AudioPart, ImagePart

logger = logging.getLogger(__name__)

MAX_IMAGES = 3
# Per image, after decoding. A phone photo is 2-5 MB; beyond this the customer is not
# sending a picture of a garment.
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# Across one message, so three large images cannot add up to an unusable request.
MAX_TOTAL_BYTES = 16 * 1024 * 1024
# Below this there is no picture in the file, whatever the header claims.
MIN_IMAGE_BYTES = 64

# The formats Gemini accepts natively. HEIC matters more than it looks: it is what an
# iPhone camera produces by default, so leaving it out would reject a large share of
# real customer photos.
ALLOWED_MIME_TYPES = (
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
    "image/gif",
)

_DATA_URL = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?:;charset=[\w-]+)?;base64,",
                       re.IGNORECASE)

# ISO base-media brands that mean "this is a HEIC/HEIF still image".
_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs",
                b"mif1", b"msf1", b"heif")


@dataclass
class RawUpload:
    """An attachment that arrived as bytes rather than as base64 in a JSON body.

    Instagram hands over a URL, so a DM photo or voice note is downloaded and turns up
    here as bytes. Re-encoding it to base64 costs a millisecond and buys the thing worth
    having: one validation path. The size limits, the type sniffing and the silence gate
    are the same ones the widget's uploads go through, rather than a second
    implementation that drifts.
    """

    data: str
    mime_type: Optional[str] = None


def from_bytes(data: bytes, mime_type: Optional[str] = None) -> RawUpload:
    """Wrap raw bytes so they can be validated like any other upload."""
    return RawUpload(data=base64.b64encode(data).decode("ascii"), mime_type=mime_type)


class AttachmentError(ValueError):
    """An upload that cannot be accepted. The message is safe to show a customer."""


def decode_images(uploads, *, max_images: int = MAX_IMAGES) -> List[ImagePart]:
    """Validate and decode uploads into ``ImagePart``s, or raise ``AttachmentError``."""
    uploads = list(uploads or [])
    if not uploads:
        return []
    if len(uploads) > max_images:
        raise AttachmentError(
            "Please send at most " + str(max_images) + " images at a time."
        )

    parts: List[ImagePart] = []
    total = 0
    for index, upload in enumerate(uploads, start=1):
        data, declared = _decode_one(upload, index)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise AttachmentError("Those images are too large in total. Please send fewer.")

        sniffed = sniff_mime_type(data)
        if sniffed is None:
            # Either not an image at all, or a format Gemini cannot read. Both are the
            # same problem from the customer's side.
            raise AttachmentError(
                "Image " + str(index) + " is not a supported image. Please send a JPEG, "
                "PNG, WebP or HEIC photo."
            )
        if declared and declared != sniffed:
            # Not fatal - browsers get this wrong, and phones relabel HEIC as JPEG - but
            # the sniffed type is the one that is trusted.
            logger.info("Upload %d declared %s but is %s", index, declared, sniffed)

        parts.append(ImagePart(data=data, mime_type=sniffed))

    return parts


def _decode_one(upload, index: int) -> Tuple[bytes, Optional[str]]:
    """Pull raw bytes and any declared type out of one upload."""
    raw = getattr(upload, "data", None)
    declared = getattr(upload, "mime_type", None)
    if raw is None and isinstance(upload, str):
        raw = upload
    if not isinstance(raw, str) or not raw.strip():
        raise AttachmentError("Image " + str(index) + " is empty.")

    raw = raw.strip()
    found = _DATA_URL.match(raw)
    if found:
        # A browser's FileReader produces "data:image/jpeg;base64,...."; take the type
        # from the prefix when the caller did not send one separately.
        declared = declared or found.group("mime")
        raw = raw[found.end():]

    # Whitespace and newlines are legal in transported base64 but not to the decoder.
    raw = re.sub(r"\s+", "", raw)

    # Reject on the encoded length first, so an enormous payload is never expanded in
    # memory just to be thrown away.
    if len(raw) > (MAX_IMAGE_BYTES * 4 // 3) + 8:
        raise AttachmentError(_too_large(index))

    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError("Image " + str(index) + " could not be read.") from exc

    if len(data) < MIN_IMAGE_BYTES:
        raise AttachmentError("Image " + str(index) + " is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise AttachmentError(_too_large(index))

    return data, (declared or "").lower().strip() or None


def _too_large(index: int) -> str:
    return ("Image " + str(index) + " is larger than "
            + str(MAX_IMAGE_BYTES // (1024 * 1024)) + " MB. Please send a smaller photo.")


def sniff_mime_type(data: bytes) -> Optional[str]:
    """The image type the bytes actually are, or ``None`` if they are not a usable image.

    The client's declared type is not trusted: browsers mislabel files, phones report
    HEIC photos as JPEG, and a caller can simply lie.
    """
    if len(data) < 12:
        return None

    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12].lower()
        if brand in _HEIF_BRANDS:
            # HEIC and HEIF share a container; Gemini accepts either label.
            return "image/heic" if brand.startswith(b"he") else "image/heif"
    return None


# --- voice notes -----------------------------------------------------------
#
# One recording per turn: a voice note is a spoken message, and a turn is one message.

MAX_VOICE_NOTES = 1
# Roughly five minutes of the 16 kHz mono WAV the widget sends, or far longer of a
# compressed recording. Past that it is not a message, it is a file.
MAX_AUDIO_BYTES = 10 * 1024 * 1024
# Shorter than this is a slip of the finger on the record button, not speech.
MIN_AUDIO_BYTES = 512

# What Gemini decodes. Ogg matters for the future rather than for today: it is what
# WhatsApp voice notes are, so the adapter will hand over ogg/opus and find it already
# supported. The widget itself converts whatever the browser recorded to WAV, which is
# the one format every browser can produce and every model reads.
ALLOWED_AUDIO_TYPES = (
    "audio/wav",
    "audio/ogg",
    "audio/mpeg",
    "audio/mp4",
    "audio/aac",
    "audio/flac",
    "audio/webm",
)


def decode_audio(uploads, *, max_clips: int = MAX_VOICE_NOTES) -> List[AudioPart]:
    """Validate and decode voice notes, or raise ``AttachmentError``."""
    uploads = list(uploads or [])
    if not uploads:
        return []
    if len(uploads) > max_clips:
        raise AttachmentError("Please send one voice note at a time.")

    parts: List[AudioPart] = []
    for index, upload in enumerate(uploads, start=1):
        data, declared = _decode_audio_one(upload, index)

        sniffed = sniff_audio_type(data)
        if sniffed is None:
            raise AttachmentError(
                "That recording is not in a format we can play. Please try again, or "
                "type your message instead."
            )
        if declared and declared != sniffed:
            logger.info("Voice note declared %s but is %s", declared, sniffed)

        if not carries_sound(data):
            # Never sent to a model: asked to transcribe silence, one will make
            # something up, and the assistant will answer a message nobody sent.
            logger.info("Rejected a silent voice note (%d bytes)", len(data))
            raise AttachmentError(
                "That recording is silent - the microphone may not have picked "
                "anything up. Please try again, or type your message."
            )

        parts.append(AudioPart(data=data, mime_type=sniffed))

    return parts


def _decode_audio_one(upload, index: int):
    """Pull raw bytes and any declared type out of one recording."""
    raw = getattr(upload, "data", None)
    declared = getattr(upload, "mime_type", None)
    if raw is None and isinstance(upload, str):
        raw = upload
    if not isinstance(raw, str) or not raw.strip():
        raise AttachmentError("That recording is empty.")

    raw = raw.strip()
    found = _DATA_URL.match(raw)
    if found:
        declared = declared or found.group("mime")
        raw = raw[found.end():]
    raw = re.sub(r"\s+", "", raw)

    # On the encoded length, before anything is expanded in memory.
    if len(raw) > (MAX_AUDIO_BYTES * 4 // 3) + 8:
        raise AttachmentError(_audio_too_large())

    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError("That recording could not be read.") from exc

    if len(data) < MIN_AUDIO_BYTES:
        raise AttachmentError("That recording is too short to hear. Please try again.")
    if len(data) > MAX_AUDIO_BYTES:
        raise AttachmentError(_audio_too_large())

    # A declared type may carry codec parameters ("audio/webm;codecs=opus").
    declared = (declared or "").lower().split(";")[0].strip() or None
    return data, declared


def _audio_too_large() -> str:
    return ("That recording is longer than we can take. Please keep it under a few "
            "minutes, or type your message instead.")


# Below this peak amplitude, as a fraction of full scale, there is nothing on the
# recording. Deliberately low: a softly spoken voice note must not be thrown away, and
# real microphone noise sits well above it. Digital silence is exactly 0.
_SILENCE_PEAK = 0.008
# And this much of the clip has to carry something, so a two-second recording that is
# silent except for one click is still nothing.
_SILENCE_FRACTION = 0.001


def carries_sound(data: bytes) -> bool:
    """Whether a recording has anything on it. ``True`` when we cannot tell.

    This exists because the model cannot be trusted to say so. Measured 2026-08-19:
    asked to transcribe one second of digital silence, gemini-3.7-flash invented a
    plausible Egyptian sentence three times out of three - and the assistant then
    answered a message the customer never sent. A prompt forbidding that did not hold.

    So silence is detected here, from the samples, before a request is ever spent on it.
    Only uncompressed WAV can be measured without decoding, which is what the widget
    sends; a compressed recording returns ``True`` rather than a guess.
    """
    samples = _pcm_samples(data)
    if samples is None:
        return True

    total = len(samples)
    if not total:
        return False

    floor = int(_SILENCE_PEAK * 32768)
    loud = sum(1 for value in samples if value > floor or value < -floor)
    return loud > max(1, int(total * _SILENCE_FRACTION))


def _pcm_samples(data: bytes) -> Optional[List[int]]:
    """16-bit samples out of a PCM WAV, or None if this is not one we can read."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None

    offset = 12
    fmt = None
    while offset + 8 <= len(data):
        name = data[offset:offset + 4]
        try:
            size = struct.unpack_from("<I", data, offset + 4)[0]
        except struct.error:
            return None
        body = offset + 8

        if name == b"fmt " and size >= 16:
            audio_format, _channels, _rate, _bps, _align, bits = struct.unpack_from(
                "<HHIIHH", data, body)
            if audio_format != 1 or bits != 16:
                # Compressed, or a width we are not going to unpack by hand.
                return None
            fmt = True
        elif name == b"data":
            if fmt is None:
                return None
            chunk = data[body:body + size] if size else data[body:]
            count = len(chunk) // 2
            if not count:
                return []
            return list(struct.unpack_from("<" + str(count) + "h", chunk))

        # Chunks are word-aligned; an odd size carries a pad byte.
        offset = body + size + (size & 1)
    return None


def sniff_audio_type(data: bytes) -> Optional[str]:
    """The audio type the bytes actually are, or ``None`` if they are not usable audio."""
    if len(data) < 12:
        return None

    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:4] == b"OggS":
        # Opus or Vorbis inside; Gemini reads the container either way.
        return "audio/ogg"
    if data[:4] == b"fLaC":
        return "audio/flac"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        # EBML - a WebM recording, which is what Chrome's MediaRecorder produces.
        return "audio/webm"
    if data[4:8] == b"ftyp":
        return "audio/mp4"
    if data[:3] == b"ID3":
        return "audio/mpeg"
    # A bare MPEG frame header: 11 sync bits, then a version that is not the reserved one.
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        if (data[1] & 0x18) != 0x08:
            # ADTS AAC shares the sync word and is told apart by its layer bits.
            return "audio/aac" if (data[1] & 0x06) == 0 else "audio/mpeg"
    return None
