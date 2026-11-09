"""Barge-in detection and handling.

Detects when the user starts speaking while the agent is still playing audio.
When barge-in occurs:
1. Clear queued audio in Twilio (stop playback)
2. Cancel the current LLM response
3. Cancel TTS generation
4. Resume listening for the user's new utterance
"""
import asyncio
import logging
from enum import Enum

logger = logging.getLogger(__name__)


class CallState(Enum):
    """State machine for call audio flow."""
    IDLE = "idle"                    # No one speaking
    USER_SPEAKING = "user_speaking"  # User is talking, agent listening
    AGENT_SPEAKING = "agent_speaking"  # Agent is playing audio
    PROCESSING = "processing"        # Processing user input (STT → LLM → TTS)


class BargeInDetector:
    """Monitors for user interruptions during agent speech."""

    def __init__(self):
        self.state = CallState.IDLE
        self._pending_marks: set[str] = set()
        self._mark_counter = 0
        self._barge_in_event = asyncio.Event()

    @property
    def is_agent_speaking(self) -> bool:
        return self.state == CallState.AGENT_SPEAKING

    def set_state(self, new_state: CallState) -> None:
        """Transition to a new state."""
        logger.debug("Call state: %s → %s", self.state.value, new_state.value)
        self.state = new_state

    def next_mark(self) -> str:
        """Generate the next mark name for Twilio playback tracking."""
        self._mark_counter += 1
        name = f"audio_{self._mark_counter}"
        self._pending_marks.add(name)
        return name

    def mark_played(self, mark_name: str) -> None:
        """Called when Twilio confirms a mark has been played."""
        self._pending_marks.discard(mark_name)
        # If no more pending marks, agent has finished speaking
        if not self._pending_marks and self.state == CallState.AGENT_SPEAKING:
            logger.info("Agent finished speaking (all marks played)")
            self.set_state(CallState.IDLE)

    def detect_barge_in(self) -> bool:
        """Check if user speech during agent playback constitutes a barge-in.

        Returns True if the agent should stop speaking.
        """
        if self.state == CallState.AGENT_SPEAKING:
            logger.info("Barge-in detected! User speaking while agent plays audio")
            self._barge_in_event.set()
            self._pending_marks.clear()
            return True
        return False

    async def wait_for_barge_in(self) -> None:
        """Wait until a barge-in event occurs."""
        self._barge_in_event.clear()
        await self._barge_in_event.wait()

    def reset(self) -> None:
        """Reset barge-in state for a new turn."""
        self._barge_in_event.clear()
        self._pending_marks.clear()
