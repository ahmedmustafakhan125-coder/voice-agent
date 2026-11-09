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


async def send_audio_to_twilio(
    websocket: WebSocket,
    stream_sid: str,
    audio_bytes: bytes,
) -> None:
    """Send audio back to Twilio as base64-encoded mulaw."""
    payload = base64.b64encode(audio_bytes).decode("ascii")
    message = {
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": payload},
    }
    await websocket.send_text(json.dumps(message))


async def send_mark_to_twilio(
    websocket: WebSocket,
    stream_sid: str,
    mark_name: str,
) -> None:
    """Send a mark event to Twilio for playback tracking."""
    message = {
        "event": "mark",
        "streamSid": stream_sid,
        "mark": {"name": mark_name},
    }
    await websocket.send_text(json.dumps(message))


async def clear_twilio_audio(
    websocket: WebSocket,
    stream_sid: str,
) -> None:
    """Tell Twilio to stop playing any queued audio (for barge-in)."""
    message = {
        "event": "clear",
        "streamSid": stream_sid,
    }
    await websocket.send_text(json.dumps(message))
