"""Conversation endpoints for the owner dashboard.

Every route needs a logged-in account, owner or staff alike (Section 5). Listing and
detail are read-only for every channel. One write path exists - replying to and taking
over an Instagram conversation - see ``app/modules/admin/conversations/__init__.py``
for why it is Instagram-only and what stays read-only.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.modules.admin.analytics.schemas import CHANNELS
from app.modules.admin.auth.router import current_account
from app.modules.admin.auth.schemas import Account
from app.modules.admin.conversations import service
from app.modules.admin.conversations.schemas import ReplyBody

router = APIRouter(prefix="/admin/api/conversations", tags=["admin-conversations"])

# Every real and placeholder channel, plus "all" - a placeholder channel's list is
# simply empty (nothing has ever opened a conversation on a channel with no
# integration), which is a plainer and more honest way to say "not connected" here than
# a special-cased response would be.
_ALLOWED_CHANNELS = CHANNELS + ("all",)


def _channel(value: str) -> Optional[str]:
    return None if value == "all" else value


@router.get("/{channel}")
def list_conversations(
    channel: str,
    limit: int = Query(default=service.DEFAULT_LIMIT, ge=1, le=200),
    sort: str = Query(default=service.DEFAULT_SORT),
    _: Account = Depends(current_account),
) -> dict:
    channel = (channel or "").strip().lower()
    if channel not in _ALLOWED_CHANNELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="channel must be one of " + ", ".join(_ALLOWED_CHANNELS),
        )
    return service.list_conversations(_channel(channel), limit, sort)


@router.get("/{channel}/{conversation_id}")
async def get_conversation(
    channel: str,
    conversation_id: str,
    _: Account = Depends(current_account),
) -> dict:
    """One conversation's transcript plus what it produced.

    ``channel`` in the path keeps this route's shape consistent with the listing route
    and with analytics; the id alone is enough to find the conversation, but asking for
    it under the wrong channel tab is refused rather than silently served, the same way
    a stale or mistyped link should be.
    """
    channel = (channel or "").strip().lower()
    if channel not in _ALLOWED_CHANNELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="channel must be one of " + ", ".join(_ALLOWED_CHANNELS),
        )

    result = await service.get_conversation(conversation_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if channel != "all" and result["channel"] != channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return result


# Deliberately not under the generic /{channel}/{conversation_id} shape the read routes
# use - the URL itself states the current restriction, and no other channel can deliver
# a reply outside its own request/response cycle yet (see the module docstring).
@router.post("/instagram/{conversation_id}/reply")
async def reply(
    conversation_id: str,
    body: ReplyBody,
    _: Account = Depends(current_account),
) -> dict:
    result = await service.reply(conversation_id, body.text)
    if result.get("error") == "not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if result.get("error") == "empty":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reply text is empty")
    if result.get("error") == "delivery_failed":
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail="Could not deliver the reply on Instagram")
    return result


@router.post("/instagram/{conversation_id}/resume")
def resume(
    conversation_id: str,
    _: Account = Depends(current_account),
) -> dict:
    result = service.resume(conversation_id)
    if result.get("error") == "not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return result
