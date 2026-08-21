"""FastAPI entrypoint.

Wires the app together and nothing more: startup/shutdown, static files, health check,
and (as they get built) each module's router. Business logic never lives here.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import PROJECT_ROOT, settings
from app.core.database import init_db
from app.modules.admin.auth import service as admin_auth_service
from app.modules.admin.router import router as admin_router
from app.modules.chat.router import router as chat_router
from app.modules.engagement.router import router as engagement_router

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("app")

STATIC_DIR = PROJECT_ROOT / "app" / "static"
# The React dashboard's production build (owner-dashboard-plan.md Section 7, step 6+).
# Same-origin on purpose - Section 6 chose "one platform, no cross-origin hosting
# complexity" over CORS, so this is `npm run build`'s output served by FastAPI itself
# rather than a second deployed service. `npm run dev` (Vite's own server, with its
# /admin/api proxy back to this app) is what a developer runs locally instead.
DASHBOARD_DIST = PROJECT_ROOT / "dashboard" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate configuration and prepare the database before serving traffic."""
    logger.info("Starting %s (%s)", settings.app_name, settings.environment)

    missing_required = settings.missing_required()
    if missing_required:
        # Fail loudly: the app cannot do its job without these.
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing_required)
        )

    for name in settings.missing_optional():
        logger.warning("%s is not set - features depending on it are disabled", name)

    logger.info("Gemini model: %s (fallbacks: %s)",
                settings.gemini_model, ", ".join(settings.gemini_fallback_models) or "none")
    if settings.shopify_configured:
        logger.info("Shopify store: %s (API %s)", settings.shopify_store, settings.shopify_api_version)

    init_db()
    admin_auth_service.bootstrap_owner()
    await _report_catalog()
    logger.info("Startup complete")
    yield
    logger.info("Shutting down")


async def _report_catalog() -> None:
    """Read the catalog once at startup so a broken Shopify setup is obvious immediately.

    Never fatal: the app must still boot and serve /health when Shopify is unreachable.
    """
    if not settings.shopify_configured:
        return
    try:
        from app.modules.catalog import service as catalog_service

        products = await catalog_service.get_catalog()
    except Exception as exc:  # noqa: BLE001 - startup diagnostics only
        logger.warning("Could not read the Shopify catalog at startup: %s", exc)
        return

    sellable = sum(1 for product in products if product.in_stock)
    logger.info("Catalog: %d active products, %d with stock", len(products), sellable)
    if products and not sellable:
        logger.warning("No product has stock - the bot will not be able to offer anything")


app = FastAPI(
    title=settings.app_name,
    description="Shopify-connected AI chatbot for Wanas Gallery.",
    version="0.1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Module routers are registered here as each module is built.
app.include_router(chat_router)  # step 2
app.include_router(engagement_router)  # Instagram comments and DMs
app.include_router(admin_router)  # owner dashboard: accounts, analytics, conversations

# Mounted *after* admin_router: Starlette matches routes in registration order, so the
# explicit /admin/api/* routes above win over this catch-all even though the mount's
# prefix also covers them. html=True serves dist/index.html for "/admin" and "/admin/"
# only - the dashboard has no client-side router with its own URLs (channel/tab
# selection is React state, not the address bar), so there is no deeper path that ever
# needs a server-side SPA-fallback the way a react-router app would.
if DASHBOARD_DIST.is_dir():
    app.mount("/admin", StaticFiles(directory=DASHBOARD_DIST, html=True), name="admin_dashboard")
else:
    logger.warning("dashboard/dist not found - run `npm run build` in dashboard/ to serve "
                   "the owner dashboard from this app; /admin/api/* still works without it")


@app.get("/health", tags=["system"])
def health() -> dict:
    """Liveness probe plus a summary of which integrations are configured."""
    return {
        "status": "ok",
        "app": settings.app_name,
        "environment": settings.environment,
        "gemini": {
            "configured": settings.gemini_configured,
            "model": settings.gemini_model,
            "fallback_models": settings.gemini_fallback_models,
        },
        "shopify": {
            "configured": settings.shopify_configured,
            "store": settings.shopify_store or None,
            "api_version": settings.shopify_api_version,
        },
        "email_configured": settings.email_configured,
        "instagram": {
            "configured": settings.instagram_configured,
            "enabled": settings.instagram_enabled,
            # Worth surfacing: with this on, every path runs and nothing is ever sent.
            # It is the difference between "quiet because nobody commented" and "quiet
            # because it was never switched on".
            "dry_run": settings.instagram_dry_run,
        },
        "online_payment": settings.online_payment_configured,
        "admin_owner_bootstrap_configured": bool(
            settings.admin_owner_username and settings.admin_owner_password
        ),
    }


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the local test chat widget."""
    return FileResponse(STATIC_DIR / "index.html")
