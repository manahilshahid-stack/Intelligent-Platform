from __future__ import annotations
from .routes.lp_auth_routes import router as lp_auth_router
from .routes.lp_chat_routes import router as lp_chat_router
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .migrations import run_migrations
from .routes.admin_routes import router as admin_router
from .routes.auth_routes import router as auth_router
from .routes.chat_routes import router as chat_router
from .routes.company_settings_routes import router as company_settings_router
from .routes.crm_routes import router as crm_router
from .routes.document_routes import router as document_router
from .routes.reporting_routes import router as reporting_router
from .routes.review_routes import router as review_router
from .routes.settings_routes import router as settings_router
from .routes.webhook_routes import router as webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Running startup migrations…")
    run_migrations()
    # Start the automatic re-index scheduler (no-op if ENABLE_SCHEDULER=0).
    try:
        from .services.scheduler import start_scheduler
        start_scheduler()
    except Exception as exc:  # never block startup on the scheduler
        log.warning("Scheduler failed to start: %s", exc)
    log.info("App ready.")
    yield
    try:
        from .services.scheduler import shutdown_scheduler
        shutdown_scheduler()
    except Exception:
        pass


app = FastAPI(title="Portfolio Intelligence Platform", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(company_settings_router)
app.include_router(crm_router)
app.include_router(reporting_router)
app.include_router(settings_router)
app.include_router(chat_router)
app.include_router(document_router)
app.include_router(review_router)
app.include_router(lp_auth_router)
app.include_router(lp_chat_router)
app.include_router(webhook_router)

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# Redirect 303 responses from require_login back to login page
@app.exception_handler(status.HTTP_303_SEE_OTHER)
async def redirect_to_login(request: Request, exc):
    return RedirectResponse(exc.headers["Location"], status_code=status.HTTP_303_SEE_OTHER)
