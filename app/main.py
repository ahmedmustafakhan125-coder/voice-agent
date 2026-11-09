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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.dashboard import router as dashboard_router
from app.api.routes import router as api_router
from app.api.webhooks import router as webhooks_router
from app.telephony.twiml import router as twiml_router
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

# CORS middleware (for dashboard frontend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routes
app.include_router(twiml_router, prefix="/twiml", tags=["TwiML"])
app.include_router(ws_router, prefix="/ws", tags=["WebSocket"])
app.include_router(webhooks_router, prefix="/webhooks", tags=["Webhooks"])
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
