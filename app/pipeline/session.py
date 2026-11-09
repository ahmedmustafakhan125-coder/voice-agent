"""CallSession — the core orchestrator for a single phone call.

Manages the real-time audio pipeline:
  Twilio audio → Deepgram STT → Gemini LLM → ElevenLabs TTS → Twilio audio

Uses asyncio queues to decouple pipeline stages and TaskGroup to manage
concurrent async tasks for each stage.
"""
import asyncio
import logging
import time

from fastapi import WebSocket

from app.pipeline.barge_in import BargeInDetector, CallState
from app.pipeline.llm import GeminiLLM
from app.pipeline.stt import DeepgramSTT
from app.pipeline.tts import ElevenLabsTTS
from app.storage.call_log import get_call_log
from app.telephony.media import (
    clear_twilio_audio,
    send_audio_to_twilio,
    send_mark_to_twilio,
)

logger = logging.getLogger(__name__)


class CallSession:
    """Orchestrates a single phone call's AI pipeline."""

    def __init__(
        self,
        websocket: WebSocket,
        stream_sid: str,
        call_sid: str,
        campaign_id: str = "",
        contact_id: str = "",
        agent_config_id: str = "",
        contact_name: str = "",
        contact_company: str = "",
    ):
        self.websocket = websocket
        self.stream_sid = stream_sid
        self.call_sid = call_sid
        self.campaign_id = campaign_id
        self.contact_id = contact_id
        self.agent_config_id = agent_config_id
        self.contact_name = contact_name
        self.contact_company = contact_company

        # Pipeline queues
        self._transcript_queue: asyncio.Queue = asyncio.Queue()
        self._sentence_queue: asyncio.Queue = asyncio.Queue()
        self._audio_out_queue: asyncio.Queue = asyncio.Queue()

        # Pipeline components
        self.stt = DeepgramSTT(on_transcript=self._transcript_queue)
        self.llm = GeminiLLM(contact_name=contact_name, company=contact_company)
        self.tts = ElevenLabsTTS(audio_out_queue=self._audio_out_queue)
        self.barge_in = BargeInDetector()

        # State
        self._running = False
        self._transcript_buffer = ""
        self._conversation_transcript: list[dict] = []
        self._start_time = 0.0
        self._qualification: dict | None = None

        # Callback for when call ends (set by campaign dialer)
        self.on_call_complete: asyncio.Queue | None = None

    async def run(self) -> None:
        """Start the call session pipeline."""
        self._running = True
        self._start_time = time.time()
        logger.info("CallSession starting: call_sid=%s", self.call_sid)

        try:
            # Connect to external services
            await self.stt.connect()
            await self.tts.connect()

            # Send personalized greeting
            if self.contact_name:
                greeting = f"Hey {self.contact_name}! This is Sarah from National Dispatch. How are you doing today?"
            else:
                greeting = "Hey there! This is Sarah from National Dispatch. How are you doing today?"
            self._conversation_transcript.append({"role": "assistant", "text": greeting})
            await self._speak(greeting)

            # Run pipeline tasks concurrently
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._process_transcripts())
                tg.create_task(self._send_audio_to_caller())

        except* Exception as eg:
            for exc in eg.exceptions:
                logger.exception("CallSession error: %s", exc)
        finally:
            await self._cleanup()

    async def receive_audio(self, audio_bytes: bytes) -> None:
        """Called by the Twilio WS handler when audio arrives from the caller."""
        if self._running:
            await self.stt.send_audio(audio_bytes)

    async def handle_mark(self, mark_name: str) -> None:
        """Called when Twilio confirms audio playback reached a mark."""
        self.barge_in.mark_played(mark_name)

    async def stop(self) -> None:
        """Stop the call session."""
        self._running = False
        # Unblock any waiting queues
        await self._transcript_queue.put(None)
        await self._sentence_queue.put(None)
        await self._audio_out_queue.put(None)

    async def _process_transcripts(self) -> None:
        """Process transcription events from Deepgram.

        Accumulates final transcripts and triggers LLM response
        when the user finishes speaking (utterance_end).
        """
        while self._running:
            try:
                event = await self._transcript_queue.get()
                if event is None:
                    break

                event_type = event.get("type", "")

                if event_type == "speech_started":
                    # User started speaking — check for barge-in
                    if self.barge_in.detect_barge_in():
                        await self._handle_barge_in()
                    self.barge_in.set_state(CallState.USER_SPEAKING)

                elif event_type == "transcript":
                    text = event.get("text", "")
                    if text:
                        self._transcript_buffer += " " + text
                        self._transcript_buffer = self._transcript_buffer.strip()

                    # If speech_final, user paused — could trigger response
                    if event.get("speech_final"):
                        logger.info("User said: %s", self._transcript_buffer)

                elif event_type == "utterance_end":
                    # User finished speaking — send to LLM
                    if self._transcript_buffer.strip():
                        user_text = self._transcript_buffer.strip()
                        self._transcript_buffer = ""

                        self._conversation_transcript.append(
                            {"role": "user", "text": user_text}
                        )
                        logger.info("Processing user utterance: %s", user_text)

                        self.barge_in.set_state(CallState.PROCESSING)
                        await self._generate_response(user_text)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error processing transcript")

    async def _generate_response(self, user_text: str) -> None:
        """Generate an AI response and speak it."""
        try:
            # Reconnect TTS for new utterance
            await self.tts.reconnect()

            # Stream LLM response with sentence chunking
            sentence_queue: asyncio.Queue = asyncio.Queue()

            # Run LLM streaming and TTS feeding concurrently
            async with asyncio.TaskGroup() as tg:
                tg.create_task(
                    self.llm.stream_response(user_text, sentence_queue)
                )
                tg.create_task(
                    self._feed_sentences_to_tts(sentence_queue)
                )

        except* Exception as eg:
            for exc in eg.exceptions:
                logger.exception("Error generating response: %s", exc)

    async def _feed_sentences_to_tts(self, sentence_queue: asyncio.Queue) -> None:
        """Read sentences from LLM and feed them to TTS."""
        full_response = ""
        self.barge_in.set_state(CallState.AGENT_SPEAKING)

        while True:
            sentence = await sentence_queue.get()
            if sentence is None:
                break

            if not self._running or self.barge_in.state != CallState.AGENT_SPEAKING:
                break

            full_response += sentence + " "
            await self.tts.send_text(sentence)

        # Flush remaining TTS audio
        await self.tts.flush()

        if full_response.strip():
            self._conversation_transcript.append(
                {"role": "assistant", "text": full_response.strip()}
            )

    async def _speak(self, text: str) -> None:
        """Speak a text message (for greeting or scripted responses)."""
        self.barge_in.set_state(CallState.AGENT_SPEAKING)
        await self.tts.send_text(text)
        await self.tts.flush()

    async def _send_audio_to_caller(self) -> None:
        """Read audio chunks from TTS output queue and send to Twilio."""
        while self._running:
            try:
                audio_bytes = await self._audio_out_queue.get()
                if audio_bytes is None:
                    # End of current TTS generation
                    # Send a mark to track when playback finishes
                    mark_name = self.barge_in.next_mark()
                    await send_mark_to_twilio(
                        self.websocket, self.stream_sid, mark_name
                    )
                    continue

                # Send audio chunk to Twilio
                await send_audio_to_twilio(
                    self.websocket, self.stream_sid, audio_bytes
                )

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error sending audio to Twilio")

    async def _handle_barge_in(self) -> None:
        """Handle user interruption during agent speech."""
        logger.info("Handling barge-in for call %s", self.call_sid)

        # 1. Clear queued audio in Twilio
        await clear_twilio_audio(self.websocket, self.stream_sid)

        # 2. Cancel current LLM response
        self.llm.cancel()

        # 3. Drain queues
        while not self._sentence_queue.empty():
            try:
                self._sentence_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        while not self._audio_out_queue.empty():
            try:
                self._audio_out_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # 4. Reset barge-in state
        self.barge_in.reset()
        self.barge_in.set_state(CallState.USER_SPEAKING)

    async def _cleanup(self) -> None:
        """Clean up all connections, qualify lead, and log call data."""
        self._running = False
        duration = time.time() - self._start_time if self._start_time else 0

        logger.info(
            "CallSession ended: call_sid=%s duration=%.1fs turns=%d",
            self.call_sid, duration, len(self._conversation_transcript),
        )

        # Close external connections
        await self.stt.close()
        await self.tts.close()

        # Log transcript for debugging
        for turn in self._conversation_transcript:
            logger.info("  [%s] %s", turn["role"], turn["text"])

        # Run post-call lead qualification
        if len(self._conversation_transcript) > 1:
            try:
                self._qualification = await self.llm.qualify_lead(
                    self._conversation_transcript
                )
                logger.info(
                    "Lead qualification: call_sid=%s result=%s",
                    self.call_sid, self._qualification,
                )
            except Exception:
                logger.exception("Failed to qualify lead for call %s", self.call_sid)

        # Persist to the call log (powers the dashboard)
        try:
            get_call_log().record_result(
                self.call_sid,
                duration=duration,
                transcript=self._conversation_transcript,
                qualification=self._qualification,
            )
        except Exception:
            logger.exception("Failed to write call log for %s", self.call_sid)

        # Notify campaign dialer that call is complete
        if self.on_call_complete:
            await self.on_call_complete.put({
                "call_sid": self.call_sid,
                "campaign_id": self.campaign_id,
                "contact_id": self.contact_id,
                "duration": duration,
                "transcript": self._conversation_transcript,
                "qualification": self._qualification,
            })

    def get_transcript(self) -> list[dict]:
        """Get the full conversation transcript."""
        return self._conversation_transcript.copy()

    def get_qualification(self) -> dict | None:
        """Get the lead qualification result."""
        return self._qualification

    def get_duration(self) -> float:
        """Get call duration in seconds."""
        if self._start_time:
            return time.time() - self._start_time
        return 0.0
