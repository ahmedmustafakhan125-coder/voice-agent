"""TwiML response generation for Twilio calls.

Returns <Connect><Stream> to tell Twilio to open a WebSocket Media Stream
back to our server so we can process audio in real-time.
"""
from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.config import settings

router = APIRouter()


@router.post("/voice")
async def handle_voice(request: Request):
    """Twilio calls this URL when a call connects.
    Returns TwiML that tells Twilio to stream audio to our WebSocket.
    """
    # Extract custom parameters from query string (set during outbound call)
    params = request.query_params
    campaign_id = params.get("campaign_id", "")
    contact_id = params.get("contact_id", "")
    agent_config_id = params.get("agent_config_id", "")

    # Build WebSocket URL — Twilio connects here for bidirectional audio
    ws_url = settings.public_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_url}/ws/media-stream"

    contact_name = params.get("contact_name", "")
    contact_company = params.get("contact_company", "")

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}">
            <Parameter name="campaign_id" value="{campaign_id}" />
            <Parameter name="contact_id" value="{contact_id}" />
            <Parameter name="agent_config_id" value="{agent_config_id}" />
            <Parameter name="contact_name" value="{contact_name}" />
            <Parameter name="contact_company" value="{contact_company}" />
        </Stream>
    </Connect>
</Response>"""

    return Response(content=twiml, media_type="application/xml")
