"""All ``/admin/api/*`` routes, assembled from each admin sub-module's own router.

``conversations`` will add its own ``router.py`` and be included here the same way
(step 5 of the build order), protected by ``auth.router.current_account`` /
``require_owner_account``.
"""

from fastapi import APIRouter

from app.modules.admin.analytics.router import router as analytics_router
from app.modules.admin.auth.router import router as auth_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(analytics_router)
