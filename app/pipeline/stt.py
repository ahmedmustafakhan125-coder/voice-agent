"""Deepgram streaming Speech-to-Text client.

Maintains a persistent WebSocket to Deepgram's streaming API.
Receives mulaw 8kHz audio bytes and emits transcription events.
"""
import asyncio
import json
import logging

import websockets

from app.config import settings

logger = logging.getLogger(__name__)

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"
MAX_RECONNECT_ATTEMPTS = 3
RECONNECT_DELAY = 1.0  # seconds


class DeepgramSTT:
    """Streaming STT client using Deepgram Nova-2."""

    def __init__(self, on_transcript: asyncio.Queue):
        """
        Args:
            on_transcript: Queue to put final transcripts into.
        """
        self.on_transcript = on_transcript
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._running = False
        self._receive_task: asyncio.Task | None = None
        self._reconnect_attempts = 0

    async def connect(self) -> None:
        """Open a streaming WebSocket to Deepgram."""
        params = (
            f"?model=nova-2"
            f"&encoding=mulaw"
            f"&sample_rate=8000"
            f"&channels=1"
            f"&punctuate=true"
            f"&interim_results=true"
            f"&endpointing=300"
            f"&vad_events=true"
            f"&utterance_end_ms=1000"
        )

        headers = {"Authorization": f"Token {settings.deepgram_api_key}"}

        self._ws = await websockets.connect(
            f"{DEEPGRAM_WS_URL}{params}",
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
        )
        self._running = True
        self._receive_task = asyncio.create_task(self._receive_loop())
        logger.info("Deepgram STT connected")

    async def send_audio(self, audio_bytes: bytes) -> None:
        """Send raw mulaw audio bytes to Deepgram for transcription."""
        if self._ws and self._running:
            try:
                await self._ws.send(audio_bytes)
            except websockets.ConnectionClosed:
                logger.warning("Deepgram connection lost, attempting reconnect")
                await self._reconnect()
                if self._ws and self._running:
                    await self._ws.send(audio_bytes)
            except Exception:
                logger.exception("Error sending audio to Deepgram")

    async def _reconnect(self) -> None:
        """Attempt to reconnect to Deepgram with exponential backoff."""
        if self._reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
            logger.error("Max Deepgram reconnect attempts reached")
            return

        self._reconnect_attempts += 1
        delay = RECONNECT_DELAY * (2 ** (self._reconnect_attempts - 1))
        logger.info("Reconnecting to Deepgram in %.1fs (attempt %d)", delay, self._reconnect_attempts)
        await asyncio.sleep(delay)

        try:
            await self.close()
            await self.connect()
            self._reconnect_attempts = 0
            logger.info("Deepgram reconnected successfully")
        except Exception:
            logger.exception("Deepgram reconnect failed")

    async def _receive_loop(self) -> None:
        """Receive transcription results from Deepgram."""
        try:
            async for raw_message in self._ws:
                message = json.loads(raw_message)
                msg_type = message.get("type", "")

                if msg_type == "Results":
                    channel = message.get("channel", {})
                    alternatives = channel.get("alternatives", [])
                    if not alternatives:
                        continue

                    transcript = alternatives[0].get("transcript", "").strip()
                    is_final = message.get("is_final", False)
                    speech_final = message.get("speech_final", False)

                    if transcript and is_final:
                        logger.info("STT final: %s (speech_final=%s)", transcript, speech_final)
                        await self.on_transcript.put({
                            "type": "transcript",
                            "text": transcript,
                            "is_final": is_final,
                            "speech_final": speech_final,
                        })

                elif msg_type == "UtteranceEnd":
                    logger.info("STT utterance end")
                    await self.on_transcript.put({
                        "type": "utterance_end",
                    })

                elif msg_type == "SpeechStarted":
                    logger.info("STT speech started (VAD)")
                    await self.on_transcript.put({
                        "type": "speech_started",
                    })

        except websockets.ConnectionClosed:
            logger.warning("Deepgram WebSocket closed")
        except Exception:
            logger.exception("Error in Deepgram receive loop")
        finally:
            self._running = False

    async def close(self) -> None:
        """Close the Deepgram WebSocket connection."""
        self._running = False
        if self._ws:
            try:
                # Send close signal to Deepgram
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        logger.info("Deepgram STT closed")
