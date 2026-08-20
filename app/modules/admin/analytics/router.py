"""Analytics endpoints for the owner dashboard - the KPI cards and the chart behind them.

Every route needs a logged-in account, owner or staff alike (Section 5: staff can view
analytics, only account management and settings are owner-only).

Step 2 of the build order asks for one channel - Web - verified against real data before
the rest are opened up (step 4). ``_ALLOWED_CHANNELS`` is that gate; it grows in step 4
rather than the route shape changing.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.modules.admin.analytics import service
from app.modules.admin.auth.router import current_account
from app.modules.admin.auth.schemas import Account

router = APIRouter(prefix="/admin/api/analytics", tags=["admin-analytics"])

# Grows to include "instagram", "all", and the placeholder channels in step 4.
_ALLOWED_CHANNELS = ("web",)


@router.get("/{channel}")
async def get_snapshot(
    channel: str,
    start: Optional[date] = Query(default=None),
    end: Optional[date] = Query(default=None),
    _: Account = Depends(current_account),
) -> dict:
    channel = (channel or "").strip().lower()
    if channel not in _ALLOWED_CHANNELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="channel must be one of " + ", ".join(_ALLOWED_CHANNELS),
        )

    if (start is None) != (end is None):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="start and end must be given together")
    if start is None:
        start, end = service.default_range()
    elif end <= start:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="end must be after start")

    result = await service.snapshot(channel, start, end)
    return result.to_dict()
