"""FastAPI application entry point.

Mounts all routes:
- /twiml/* — TwiML responses for Twilio
- /ws/* — WebSocket endpoints for Twilio Media Streams
- /api/* — REST API for campaign management and dashboard
"""
import logging
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.auth import router as auth_router
from app.api.dashboard import router as dashboard_router
from app.api.routes import router as api_router
from app.api.webhooks import router as webhooks_router
from app.telephony.twiml import router as twiml_router
from app.config import settings
from app.core import auth
from app.telephony.twilio_ws import router as ws_router

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(
    title="Voice Agent — Trucking Dispatch",
    description="AI-powered voice agent for cold-calling truckers",
    version="0.1.0",
)

# CORS is only needed for a dashboard hosted on another origin.
_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def require_dashboard_login(request: Request, call_next):
    """Protect /api/* (except /api/auth/*). Twilio routes stay public.

    Before a password exists, only direct requests from this machine are
    allowed, so a server exposed via ngrok isn't open to the world.
    """
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/auth/"):
        if auth.password_configured():
            allowed = auth.is_authenticated(request)
        else:
            allowed = auth.is_local_request(request)
        if not allowed:
            return JSONResponse(
                {"detail": "Not authenticated", "password_set": auth.password_configured()},
                status_code=401,
            )
    return await call_next(request)

# Mount routes
app.include_router(twiml_router, prefix="/twiml", tags=["TwiML"])
app.include_router(ws_router, prefix="/ws", tags=["WebSocket"])
app.include_router(webhooks_router, prefix="/webhooks", tags=["Webhooks"])
app.include_router(auth_router, prefix="/api", tags=["Auth"])
app.include_router(api_router, prefix="/api", tags=["API"])
app.include_router(dashboard_router, prefix="/api", tags=["Dashboard"])


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "healthy"}


# Mount static files AFTER all routes so /api/* takes precedence
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
