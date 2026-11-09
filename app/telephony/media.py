"""Helpers for sending messages back over a Twilio Media Stream WebSocket.

Kept separate from twilio_ws.py so the pipeline (session.py) can use them
without a circular import (twilio_ws imports session to create CallSessions).
"""
import base64
import json

from fastapi import WebSocket


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
