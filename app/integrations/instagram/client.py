"""Raw Instagram Graph API client.

A dumb wrapper, on the same terms as ``shopify/client.py``: it knows how to
authenticate, how to shape a Graph API call, how to survive Meta's throttling and how to
hand back the raw payload. It does not know what makes a comment "important", when a
reply should be public, or what the bot ought to say. That lives in
``modules/engagement``.

Two things here are not merely stylistic:

**Writes are never retried.** A public reply, a like, a DM and a private reply are all
outward-facing, and none of Meta's send endpoints are idempotent. A retry after an
ambiguous failure is how one comment gets answered twice in public. Reads retry freely.

**The client paces itself.** Meta publishes generous ceilings, but a burst of automated
public activity reads as spam whatever the documented limit says, and a runaway loop is
much cheaper to notice at 200 actions an hour than at 750.
"""

import asyncio
import logging
import random
import time
from collections import deque
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.instagram.com"


class InstagramError(Exception):
    """Any Instagram failure."""


class InstagramNotConfigured(InstagramError):
    """Token, account id or app secret is missing."""


class InstagramAuthError(InstagramError):
    """Token rejected, expired or lacking a permission - retrying will not help.

    Worth its own type because the likeliest cause is the 60-day token quietly
    expiring, which otherwise looks like the whole feature simply going silent.
    """


class InstagramThrottled(InstagramError):
    """Rate limit hit; worth retrying after a pause."""


class InstagramUnavailable(InstagramError):
    """Transport or 5xx failure; worth retrying."""


class InstagramRejected(InstagramError):
    """Meta understood the request and refused it.

    The caller's problem to interpret, not an outage to apologise for. Subcode 2534014 is
    the one that matters most: a comment gets exactly one private reply, ever, and that
    is Meta saying this comment has already had it.
    """

    def __init__(self, message: str, code: Optional[int] = None,
                 subcode: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.subcode = subcode

    @property
    def private_reply_already_sent(self) -> bool:
        return self.subcode == 2534014


# Meta's error codes that mean "slow down" rather than "you are wrong".
_THROTTLE_CODES = {4, 17, 32, 613}
# ...and the ones that mean the token itself is the problem.
_AUTH_CODES = {102, 190, 200, 803}


class _Pace:
    """A rolling one-hour cap on outward actions, shared by every client instance.

    Deliberately a wait rather than a refusal: a comment that arrives during a busy hour
    should be answered late, not dropped. If the wait would be absurd the caller gets a
    throttle error instead, because by then something is looping rather than trading.
    """

    def __init__(self) -> None:
        self._done: deque = deque()
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        limit = max(1, settings.instagram_max_actions_per_hour)
        async with self._lock:
            self._forget_old()
            if len(self._done) >= limit:
                wait = 3600 - (time.monotonic() - self._done[0])
                if wait > 300:
                    raise InstagramThrottled(
                        "The hourly Instagram action cap (%d) stays full for another "
                        "%.0f minutes" % (limit, wait / 60)
                    )
                logger.warning("Instagram action cap reached; waiting %.0fs", wait)
                await asyncio.sleep(max(0.0, wait))
                self._forget_old()
            self._done.append(time.monotonic())

    def _forget_old(self) -> None:
        now = time.monotonic()
        while self._done and now - self._done[0] > 3600:
            self._done.popleft()

    def reset(self) -> None:
        self._done.clear()


_pace = _Pace()


def reset_pace() -> None:
    """Drop the recorded actions. For tests, which must not inherit each other's rate."""
    _pace.reset()


def split_message(text: str, limit: Optional[int] = None) -> List[str]:
    """Break a reply into pieces Instagram will accept whole.

    Instagram truncates a DM past its limit, and a truncated reply is worse than two
    messages - it can cut a price or a phone number in half. Splits on paragraph, then
    sentence, then word boundaries so each piece still reads as something a person wrote.
    """
    limit = max(1, limit or settings.instagram_message_char_limit)
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []

    pieces: List[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        # The cut goes on the far side of the punctuation, so a sentence keeps its full
        # stop instead of starting the next message with one.
        cut = 0
        for separator in ("\n\n", ". ", "؟ ", "? ", "! ", "، ", "\n"):
            found = window.rfind(separator)
            if found >= 0:
                cut = max(cut, found + len(separator))
        if cut < limit // 4:
            cut = window.rfind(" ") + 1
        if cut < limit // 4:
            # One unbroken run longer than the limit - a hard cut is all that is left.
            cut = limit
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return [piece for piece in pieces if piece]


class InstagramClient:
    """Thin async wrapper over the Instagram Graph API."""

    def __init__(self, token: Optional[str] = None,
                 account_id: Optional[str] = None) -> None:
        self._token = token if token is not None else settings.instagram_access_token
        self._account_id = (account_id if account_id is not None
                            else settings.instagram_business_account_id)
        if not self._token or not self._account_id:
            raise InstagramNotConfigured(
                "INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_BUSINESS_ACCOUNT_ID must both be set"
            )
        self._base = GRAPH_HOST + "/" + settings.instagram_graph_version

    @property
    def account_id(self) -> str:
        """Our own Instagram id - what the loop guards compare against."""
        return self._account_id

    # -- reads -----------------------------------------------------------
    async def get_media(self, media_id: str) -> Dict[str, Any]:
        """A post or reel: its caption, media type and image URL.

        ``media_url`` on a video is the video file, so ``thumbnail_url`` is asked for
        too - a reel still has a frame worth matching against the catalog.
        """
        return await self._request(
            "GET", "/" + media_id,
            params={"fields": "id,caption,media_type,media_url,thumbnail_url,permalink"},
        )

    async def get_comment(self, comment_id: str) -> Dict[str, Any]:
        """One comment, with who wrote it and what it sits under."""
        return await self._request(
            "GET", "/" + comment_id,
            params={"fields": "id,text,timestamp,username,from,media{id,caption}"},
        )

    async def download(self, url: str, max_bytes: int = 12 * 1024 * 1024) -> bytes:
        """Fetch a file Meta is hosting - a post's image, or a DM attachment.

        These URLs are pre-signed and short-lived, so no token is attached. The size cap
        lives here rather than in the caller because this is where bytes from outside
        enter the process.
        """
        timeout = httpx.Timeout(settings.instagram_timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
                response = await http.get(url)
                if response.status_code >= 400:
                    raise InstagramUnavailable(
                        "Downloading media failed with HTTP " + str(response.status_code))
                data = response.content
        except httpx.HTTPError as exc:
            raise InstagramUnavailable(type(exc).__name__ + ": " + str(exc)) from exc

        if len(data) > max_bytes:
            raise InstagramRejected("That file is too large to read (%d bytes)" % len(data))
        return data

    # -- writes ----------------------------------------------------------
    async def reply_to_comment(self, comment_id: str, message: str) -> Dict[str, Any]:
        """Post a public reply under a comment."""
        await _pace.take()
        return await self._request("POST", "/" + comment_id + "/replies",
                                   data={"message": message}, max_attempts=1)

    async def like_comment(self, comment_id: str) -> bool:
        """Like a comment on one of our own posts. Meta added this in April 2026."""
        await _pace.take()
        result = await self._request("POST", "/" + comment_id + "/likes", max_attempts=1)
        return bool(result.get("success", True))

    async def send_private_reply(self, comment_id: str, message: str) -> Dict[str, Any]:
        """Open a DM off the back of a comment.

        Two hard limits, both Meta's: within seven days of the comment, and once per
        comment for all time. A second attempt comes back as ``InstagramRejected`` with
        ``private_reply_already_sent`` set.
        """
        await _pace.take()
        return await self._request(
            "POST", "/" + self._account_id + "/messages",
            json={"recipient": {"comment_id": comment_id},
                  "message": {"text": message}},
            max_attempts=1,
        )

    async def send_message(self, recipient_id: str, text: str) -> List[str]:
        """Send a DM, split into as many messages as its length needs.

        Returns the message ids. A failure part-way through is raised rather than
        swallowed: the caller must not assume the whole reply landed, and must not
        resend the pieces that did.
        """
        sent: List[str] = []
        for piece in split_message(text):
            await _pace.take()
            result = await self._request(
                "POST", "/" + self._account_id + "/messages",
                json={"recipient": {"id": recipient_id}, "message": {"text": piece}},
                max_attempts=1,
            )
            sent.append(str(result.get("message_id", "")))
        return sent

    async def ping(self) -> Dict[str, Any]:
        """Read our own profile. Used by /health to prove the token is still alive."""
        return await self._request("GET", "/" + self._account_id,
                                   params={"fields": "id,username"})

    # -- transport -------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        max_attempts: int = 3,
    ) -> Dict[str, Any]:
        """One Graph API call.

        ``max_attempts`` is 1 for every send: none of Meta's write endpoints are
        idempotent, so a retry after an ambiguous failure is how a customer gets
        answered twice.
        """
        url = self._base + path
        params = dict(params or {})
        params["access_token"] = self._token

        last_error: InstagramError = InstagramUnavailable("no attempt was made")
        timeout = httpx.Timeout(settings.instagram_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as http:
            for attempt in range(1, max_attempts + 1):
                try:
                    response = await http.request(method, url, params=params,
                                                  data=data, json=json)
                    return _parse(response)
                except (InstagramThrottled, InstagramUnavailable) as exc:
                    last_error = exc
                except httpx.HTTPError as exc:
                    last_error = InstagramUnavailable(type(exc).__name__ + ": " + str(exc))

                if attempt == max_attempts:
                    break
                delay = min(8.0, 0.8 * (2 ** (attempt - 1))) + random.uniform(0, 0.3)
                logger.warning("Instagram attempt %d/%d failed (%s); retrying in %.1fs",
                               attempt, max_attempts, last_error, delay)
                await asyncio.sleep(delay)

        raise last_error


def _parse(response: httpx.Response) -> Dict[str, Any]:
    """Turn a Graph API response into data, or into the right kind of error."""
    try:
        payload = response.json()
    except ValueError:
        if response.status_code >= 500:
            raise InstagramUnavailable("HTTP " + str(response.status_code)) from None
        raise InstagramError(
            "Instagram returned a non-JSON response (HTTP "
            + str(response.status_code) + ")"
        ) from None

    error = payload.get("error") if isinstance(payload, dict) else None
    if error:
        code = error.get("code")
        subcode = error.get("error_subcode")
        message = str(error.get("message") or error)
        if code in _AUTH_CODES:
            raise InstagramAuthError(message + " (code " + str(code) + ")")
        if code in _THROTTLE_CODES:
            raise InstagramThrottled(message)
        if response.status_code >= 500:
            raise InstagramUnavailable(message)
        raise InstagramRejected(message, code=code, subcode=subcode)

    if response.status_code >= 500:
        raise InstagramUnavailable("HTTP " + str(response.status_code))
    if response.status_code >= 400:
        raise InstagramRejected("HTTP " + str(response.status_code))
    return payload if isinstance(payload, dict) else {"data": payload}
