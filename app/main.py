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
from app.modules.chat.router import router as chat_router
from app.modules.dashboard.router import router as dashboard_router

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("app")

STATIC_DIR = PROJECT_ROOT / "app" / "static"


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
app.include_router(dashboard_router)  # owner-facing view


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
        "online_payment": settings.online_payment_configured,
        "dashboard_enabled": settings.dashboard_enabled,
    }


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the local test chat widget."""
    return FileResponse(STATIC_DIR / "index.html")
