"""Meta's webhook endpoints.

Transport only, on the same terms as ``chat/router.py``: prove the delivery is genuine,
turn it into events, claim each one, and hand the work to ``service.py``.

Two rules shape this file:

**Answer fast, work afterwards.** Meta expects a 200 within seconds and redelivers
anything slower - and a redelivery of work already in flight is a second public reply.
So the response goes out first and the handling runs in a background task.

**Return 200 even when we do nothing.** Meta disables a subscription that keeps
erroring, and a disabled subscription is silent in exactly the way nobody notices. The
one exception is a bad signature, which is refused outright: that is not our webhook.
"""

import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Request, Response, status

from app.core.config import settings
from app.modules.engagement import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/instagram", tags=["instagram"])


@router.get("", include_in_schema=False)
async def verify(request: Request) -> Response:
    """Meta's subscription handshake: echo the challenge if the token matches."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge") or ""

    expected = settings.instagram_webhook_verify_token
    if mode == "subscribe" and expected and hmac.compare_digest(token or "", expected):
        logger.info("Instagram webhook subscription verified")
        return Response(content=challenge, media_type="text/plain")

    logger.warning("Rejected an Instagram webhook verification (mode=%r)", mode)
    return Response(status_code=status.HTTP_403_FORBIDDEN)


@router.post("", include_in_schema=False)
async def receive(request: Request, background: BackgroundTasks) -> Response:
    """One webhook delivery: verify it, claim what is in it, then answer."""
    raw = await request.body()
    signature = request.headers.get("x-hub-signature-256", "")

    if not _signature_ok(raw, signature):
        # Not a Meta delivery. Without this check anyone who found the URL could post a
        # fabricated comment and have the bot reply to it in public, or DM a stranger.
        logger.warning("Rejected an Instagram webhook with a bad signature")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is Meta's problem, not an outage
        logger.warning("Instagram sent a webhook body that is not JSON")
        return Response(status_code=status.HTTP_200_OK)

    try:
        service.accept(payload, background)
    except Exception:  # noqa: BLE001 - never let a parse bug disable the subscription
        logger.exception("Could not accept an Instagram webhook delivery")

    return Response(status_code=status.HTTP_200_OK)


def _signature_ok(body: bytes, header: str) -> bool:
    """Whether this body really came from our Meta app.

    Computed over the raw bytes, because re-serialising the JSON would change them and
    the signature is over what was sent. Compared in constant time.
    """
    secret = settings.instagram_app_secret
    if not secret:
        # Fails closed: an unset secret must not become "accept everything".
        return False
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])
