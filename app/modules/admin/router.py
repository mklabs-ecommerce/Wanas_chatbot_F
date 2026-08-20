"""All ``/admin/api/*`` routes, assembled from each admin sub-module's own router."""

from fastapi import APIRouter

from app.modules.admin.analytics.router import router as analytics_router
from app.modules.admin.auth.router import router as auth_router
from app.modules.admin.conversations.router import router as conversations_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(analytics_router)
router.include_router(conversations_router)
