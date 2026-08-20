"""All ``/admin/api/*`` routes, assembled from each admin sub-module's own router.

Auth is the only piece that exists yet (Section 7, step 1 of the build order).
``analytics`` and ``conversations`` will each add their own ``router.py`` and be
included here the same way, protected by ``auth.router.current_account`` /
``require_owner_account``.
"""

from fastapi import APIRouter

from app.modules.admin.auth.router import router as auth_router

router = APIRouter()
router.include_router(auth_router)
