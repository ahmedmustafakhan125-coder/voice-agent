"""Twilio Media Streams WebSocket handler.

Accepts the WebSocket connection from Twilio, decodes incoming audio,
creates a CallSession to process it through STT → LLM → TTS,
and sends synthesized audio back to Twilio.
"""
import asyncio
import base64
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.pipeline.session import CallSession
from app.telephony.media import (  # noqa: F401  (re-exported for compatibility)
    clear_twilio_audio,
    send_audio_to_twilio,
    send_mark_to_twilio,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    """Handle Twilio Media Stream WebSocket connection."""
    await websocket.accept()
    logger.info("Twilio WebSocket connected")

    session: CallSession | None = None
    stream_sid: str = ""

    try:
        async for raw_message in websocket.iter_text():
            message = json.loads(raw_message)
            event = message.get("event")

            if event == "connected":
                logger.info("Twilio media stream connected")

            elif event == "start":
                # Extract stream metadata and custom parameters
                start_data = message.get("start", {})
                stream_sid = start_data.get("streamSid", "")
                call_sid = start_data.get("callSid", "")
                custom_params = start_data.get("customParameters", {})

                logger.info(
                    "Stream started: stream_sid=%s call_sid=%s params=%s",
                    stream_sid, call_sid, custom_params,
                )

                # Create and start the call session
                session = CallSession(
                    websocket=websocket,
                    stream_sid=stream_sid,
                    call_sid=call_sid,
                    campaign_id=custom_params.get("campaign_id", ""),
                    contact_id=custom_params.get("contact_id", ""),
                    agent_config_id=custom_params.get("agent_config_id", ""),
                    contact_name=custom_params.get("contact_name", ""),
                    contact_company=custom_params.get("contact_company", ""),
                )
                # Start the pipeline in the background
                asyncio.create_task(session.run())

            elif event == "media":
                if session:
                    # Decode base64 mulaw audio and feed to the session
                    payload = message["media"]["payload"]
                    audio_bytes = base64.b64decode(payload)
                    await session.receive_audio(audio_bytes)

            elif event == "mark":
                if session:
                    # Twilio confirms audio playback reached a mark
                    mark_name = message.get("mark", {}).get("name", "")
                    await session.handle_mark(mark_name)

            elif event == "stop":
                logger.info("Twilio stream stopped: stream_sid=%s", stream_sid)
                if session:
                    await session.stop()
                break

    except WebSocketDisconnect:
        logger.info("Twilio WebSocket disconnected: stream_sid=%s", stream_sid)
    except Exception:
        logger.exception("Error in Twilio WebSocket handler")
    finally:
        if session:
            await session.stop()
        logger.info("Twilio WebSocket handler finished: stream_sid=%s", stream_sid)

