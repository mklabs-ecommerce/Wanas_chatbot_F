"""Turning what a browser uploads into image bytes the model can be shown.

Images arrive base64-encoded inside the JSON body rather than as a multipart upload.
That keeps one request shape for every channel - a WhatsApp adapter will hand over
downloaded bytes, not a browser file part - and keeps the endpoint testable with a plain
JSON client.

Everything here is a guard. The uploader is a stranger, so the declared content type is
not believed: the bytes are sniffed, and anything that is not a real image Gemini
accepts is rejected before it reaches the model.
"""

import base64
import binascii
import logging
import re
from typing import List, Optional, Tuple

from app.integrations.llm_types import ImagePart

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
