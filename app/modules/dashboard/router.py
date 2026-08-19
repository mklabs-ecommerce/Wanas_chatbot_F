"""The owner-facing dashboard: one page, and the JSON behind it.

Transport and access control only. What to show is decided in ``service.py``.

**Everything here is customer personal data** - real names, phone numbers, delivery
addresses and whole conversations. So:

- the dashboard does not exist unless ``DASHBOARD_TOKEN`` is set (fail closed);
- every route, including the page itself, checks the token;
- the token is compared in constant time.

This is a shared secret, not a login. It is enough for one owner on one machine, and it
is not enough to hand round a team - if this ever needs more than one reader, it needs
real accounts.
"""

import logging
import secrets
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import HTMLResponse

from app.core.config import settings
from app.modules.dashboard import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_PAGE = Path(__file__).with_name("page.html")


def _authorise(token: Optional[str], header_token: Optional[str]) -> None:
    """Let the request through, or refuse it. Never says which part was wrong."""
    if not settings.dashboard_enabled:
        # 404 rather than 403: an unconfigured dashboard should not advertise itself.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    supplied = header_token or token or ""
    if not secrets.compare_digest(supplied, settings.dashboard_token.strip()):
        logger.warning("Dashboard access refused")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Not authorised")


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def page(token: Optional[str] = Query(default=None),
         x_dashboard_token: Optional[str] = Header(default=None)) -> HTMLResponse:
    """The dashboard itself. Open /dashboard?token=... in a browser."""
    _authorise(token, x_dashboard_token)
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"))


@router.get("/api/conversations")
def conversations(limit: int = Query(default=service.DEFAULT_CONVERSATION_LIMIT,
                                     ge=1, le=200),
                  sort: str = Query(default=service.DEFAULT_SORT),
                  token: Optional[str] = Query(default=None),
                  x_dashboard_token: Optional[str] = Header(default=None)) -> dict:
    """Every recent conversation, with counts of what each produced.

    ``sort`` is one of ``service.SORTS``; anything else falls back to the default rather
    than erroring, so a stale bookmark still shows the page.
    """
    _authorise(token, x_dashboard_token)
    return service.overview(limit, sort)


@router.get("/api/conversations/{conversation_id}")
async def conversation(conversation_id: str,
                       token: Optional[str] = Query(default=None),
                       x_dashboard_token: Optional[str] = Header(default=None)) -> dict:
    """One conversation, its transcript, and the orders, tickets and feedback from it."""
    _authorise(token, x_dashboard_token)
    return await service.conversation(conversation_id)
