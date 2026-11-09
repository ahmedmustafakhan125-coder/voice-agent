"""ElevenLabs streaming Text-to-Speech client.

Uses the ElevenLabs WebSocket API directly (not the SDK) for fine-grained
control over streaming, output format (ulaw_8000), and latency optimization.

Output is raw mulaw 8kHz audio — directly compatible with Twilio Media Streams,
no transcoding needed.
"""
import asyncio
import base64
import json
import logging

import websockets

from app.config import settings

logger = logging.getLogger(__name__)

ELEVENLABS_WS_URL = "wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input"
MAX_RECONNECT_ATTEMPTS = 3
RECONNECT_DELAY = 0.5  # seconds


class ElevenLabsTTS:
    """Streaming TTS client using ElevenLabs WebSocket API."""

    def __init__(self, audio_out_queue: asyncio.Queue):
        """
        Args:
            audio_out_queue: Queue to put raw mulaw audio chunks into.
        """
        self.audio_out_queue = audio_out_queue
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._running = False
        self._receive_task: asyncio.Task | None = None

    async def connect(self) -> None:
        """Open a streaming WebSocket to ElevenLabs."""
        voice_id = settings.elevenlabs_voice_id
        url = ELEVENLABS_WS_URL.format(voice_id=voice_id)

        # Query parameters for latency optimization
        params = (
            f"?model_id=eleven_flash_v2_5"
            f"&output_format=ulaw_8000"
            f"&optimize_streaming_latency=3"
        )

        self._ws = await websockets.connect(
            f"{url}{params}",
            ping_interval=20,
            ping_timeout=10,
        )
        self._running = True

        # Send initial configuration (BOS - beginning of stream)
        bos_message = {
            "text": " ",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
            },
            "xi_api_key": settings.elevenlabs_api_key,
            "generation_config": {
                "chunk_length_schedule": [50],  # Fast first chunk
            },
        }
        await self._ws.send(json.dumps(bos_message))

        # Start receiving audio chunks
        self._receive_task = asyncio.create_task(self._receive_loop())
        logger.info("ElevenLabs TTS connected (voice=%s)", voice_id)

    async def send_text(self, text: str) -> None:
        """Send a text chunk to ElevenLabs for synthesis.

        Call this for each sentence as it comes from the LLM.
        """
        if self._ws and self._running:
            try:
                message = {
                    "text": text + " ",  # Space helps with word boundaries
                    "try_trigger_generation": True,
                }
                await self._ws.send(json.dumps(message))
            except websockets.ConnectionClosed:
                logger.warning("ElevenLabs connection lost during send, reconnecting")
                await self.reconnect()
                if self._ws and self._running:
                    message = {
                        "text": text + " ",
                        "try_trigger_generation": True,
                    }
                    await self._ws.send(json.dumps(message))
            except Exception:
                logger.exception("Error sending text to ElevenLabs")

    async def flush(self) -> None:
        """Signal end of text input — flush remaining audio.

        Send empty string to trigger generation of any remaining text.
        """
        if self._ws and self._running:
            try:
                message = {"text": ""}
                await self._ws.send(json.dumps(message))
            except Exception:
                logger.exception("Error flushing ElevenLabs TTS")

    async def _receive_loop(self) -> None:
        """Receive audio chunks from ElevenLabs and put them in the output queue."""
        try:
            async for raw_message in self._ws:
                response = json.loads(raw_message)

                # Audio chunk received
                audio_b64 = response.get("audio")
                if audio_b64:
                    audio_bytes = base64.b64decode(audio_b64)
                    if audio_bytes:
                        await self.audio_out_queue.put(audio_bytes)

                # Check if this is the final chunk
                if response.get("isFinal"):
                    logger.debug("ElevenLabs TTS generation complete")
                    await self.audio_out_queue.put(None)  # Signal end of audio

        except websockets.ConnectionClosed:
            logger.warning("ElevenLabs WebSocket closed")
        except Exception:
            logger.exception("Error in ElevenLabs receive loop")
        finally:
            self._running = False

    async def close(self) -> None:
        """Close the ElevenLabs WebSocket connection."""
        self._running = False
        if self._ws:
            try:
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
        logger.info("ElevenLabs TTS closed")

    async def reconnect(self) -> None:
        """Reconnect TTS for a new utterance (new WebSocket connection)."""
        await self.close()
        await self.connect()
