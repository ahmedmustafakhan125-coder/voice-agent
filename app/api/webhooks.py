"""Twilio status callback webhook handler.

Receives call status updates from Twilio (initiated, ringing, answered, completed)
and logs them for tracking.
"""
import logging

from fastapi import APIRouter, Form

router = APIRouter()
logger = logging.getLogger(__name__)

# In-memory call status store (replaced by Google Sheets in Phase 3)
call_statuses: dict[str, dict] = {}


@router.post("/twilio/status")
async def twilio_status_callback(
    CallSid: str = Form(""),
    CallStatus: str = Form(""),
    From: str = Form(""),
    To: str = Form(""),
    Duration: str = Form(""),
    CallDuration: str = Form(""),
):
    """Handle Twilio call status callbacks.

    Twilio POSTs here when call status changes:
    initiated → ringing → answered → completed
    """
    logger.info(
        "Twilio status: sid=%s status=%s from=%s to=%s duration=%s",
        CallSid, CallStatus, From, To, Duration or CallDuration,
    )

    call_statuses[CallSid] = {
        "call_sid": CallSid,
        "status": CallStatus,
        "from": From,
        "to": To,
        "duration": Duration or CallDuration,
    }

    return {"status": "ok"}


def get_call_status(call_sid: str) -> dict | None:
    """Look up the latest status for a call."""
    return call_statuses.get(call_sid)
