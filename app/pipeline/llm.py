"""Google Gemini streaming LLM client with sentence-boundary chunking.

Streams responses from Gemini and splits them at sentence boundaries
so TTS can start synthesizing the first sentence while the LLM is
still generating the rest. This dramatically reduces perceived latency.
"""
import asyncio
import logging

import google.generativeai as genai

from app.config import settings

logger = logging.getLogger(__name__)

# Default system prompt for dispatching sales
DEFAULT_SYSTEM_PROMPT = """You are a professional and friendly sales representative for a trucking dispatch company. Your name is Sarah.

Your goal is to pitch dispatching services to truck drivers/owners. Be conversational, warm, and natural — like a real person on the phone.

Key talking points:
- We help truckers find the best-paying loads and handle all the paperwork
- Our dispatchers are available 24/7 to support drivers on the road
- We charge a competitive rate and truckers typically see 15-20% higher revenue
- We handle broker negotiations, rate confirmations, and load planning

During the call:
1. Start with a brief, friendly greeting and introduction
2. Ask if they're currently working with a dispatcher
3. Pitch the benefits naturally based on their response
4. Handle objections calmly and professionally
5. If they're interested, ask for their email to send over the paperwork and agreement
6. If not interested, thank them for their time and end politely

Important rules:
- Keep responses SHORT (1-3 sentences max) — this is a phone call, not an essay
- Sound natural and conversational, not robotic or scripted
- If they say they're busy, offer to call back at a better time
- If they ask about specific rates, say you'd love to go over the details via email
- Always be respectful and professional
- Never be pushy or aggressive
- When you collect their email, repeat it back to confirm you got it right
- Use filler words occasionally like "yeah", "sure", "absolutely" to sound human"""

# Prompt for post-call lead qualification
QUALIFICATION_PROMPT = """Analyze this phone call transcript between a sales agent and a trucker.
Extract the following as JSON:

{
  "interest_level": "interested" | "maybe" | "not_interested",
  "email_collected": true | false,
  "email_address": "<email or null>",
  "company_name": "<company name or null>",
  "truck_type": "<truck type if mentioned or null>",
  "fleet_size": "<number if mentioned or null>",
  "objections": ["list of objections raised"],
  "outcome": "qualified" | "follow_up" | "not_interested" | "no_answer" | "voicemail",
  "summary": "<1-2 sentence summary of the call>"
}

Rules:
- "qualified" = interested AND email collected
- "follow_up" = interested but no email, or "maybe"
- Only extract email if the trucker explicitly stated it
- Be accurate — do not hallucinate information not in the transcript

Transcript:
{transcript}"""


def build_system_prompt(
    contact_name: str = "",
    company: str = "",
    custom_prompt: str = "",
) -> str:
    """Build a personalized system prompt with contact data injected.

    Args:
        contact_name: Trucker's name (if known from Google Sheets).
        company: Trucker's company name (if known).
        custom_prompt: Override the default prompt entirely.
    """
    if custom_prompt:
        prompt = custom_prompt
    else:
        prompt = DEFAULT_SYSTEM_PROMPT

    # Inject contact context if available
    if contact_name or company:
        context = "\n\nContext about this call:"
        if contact_name:
            context += f"\n- The trucker's name is {contact_name}. Use their name naturally in conversation."
        if company:
            context += f"\n- Their company is {company}."
        prompt += context

    return prompt


class GeminiLLM:
    """Streaming Gemini chat client with sentence chunking for TTS."""

    def __init__(
        self,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        contact_name: str = "",
        company: str = "",
    ):
        genai.configure(api_key=settings.gemini_api_key)
        prompt = build_system_prompt(contact_name, company, system_prompt)
        self.model = genai.GenerativeModel(
            model_name="gemini-2.0-flash",
            system_instruction=prompt,
        )
        self.chat = self.model.start_chat(history=[])
        self._cancelled = False
        self._contact_name = contact_name

    def cancel(self) -> None:
        """Cancel the current streaming response (for barge-in)."""
        self._cancelled = True

    async def stream_response(
        self,
        user_message: str,
        sentence_queue: asyncio.Queue,
    ) -> str:
        """Stream a response from Gemini, chunking into sentences for TTS.

        Splits streamed tokens on sentence boundaries (. ! ? ,) and puts
        each sentence into the queue immediately so TTS can start early.

        Args:
            user_message: The user's transcribed speech.
            sentence_queue: Queue to put sentence chunks into for TTS.

        Returns:
            The full response text.
        """
        self._cancelled = False
        buffer = ""
        full_response = ""

        try:
            # Use generate_content with stream=True
            response = await asyncio.to_thread(
                self.model.generate_content,
                self.chat.history + [{"role": "user", "parts": [user_message]}],
                stream=True,
            )

            for chunk in response:
                if self._cancelled:
                    logger.info("LLM response cancelled (barge-in)")
                    break

                text = chunk.text if chunk.text else ""
                if not text:
                    continue

                buffer += text
                full_response += text

                # Split on sentence boundaries for natural TTS pacing
                sentences, buffer = _split_sentences(buffer)
                for sentence in sentences:
                    if sentence.strip():
                        await sentence_queue.put(sentence.strip())

            # Flush remaining buffer
            if buffer.strip() and not self._cancelled:
                await sentence_queue.put(buffer.strip())

            # Signal end of response
            await sentence_queue.put(None)

            # Update chat history with the full exchange
            if not self._cancelled:
                self.chat.history.append({"role": "user", "parts": [user_message]})
                self.chat.history.append({"role": "model", "parts": [full_response]})

        except Exception:
            logger.exception("Error streaming Gemini response")
            await sentence_queue.put(None)

        return full_response

    async def qualify_lead(self, transcript: list[dict]) -> dict:
        """Analyze a call transcript and extract lead qualification data.

        Args:
            transcript: List of {role, text} dicts from the call.

        Returns:
            Dict with interest_level, email, outcome, summary, etc.
        """
        # Format transcript for the prompt
        formatted = "\n".join(
            f"{'Agent' if t['role'] == 'assistant' else 'Trucker'}: {t['text']}"
            for t in transcript
        )

        prompt = QUALIFICATION_PROMPT.format(transcript=formatted)

        try:
            response = await asyncio.to_thread(
                self.model.generate_content, prompt
            )
            text = response.text.strip()

            # Parse JSON from response (handle markdown code blocks)
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            import json
            return json.loads(text)

        except Exception:
            logger.exception("Error qualifying lead")
            return {
                "interest_level": "unknown",
                "email_collected": False,
                "email_address": None,
                "outcome": "error",
                "summary": "Failed to analyze transcript",
            }


def _split_sentences(text: str) -> tuple[list[str], str]:
    """Split text into complete sentences and a remaining buffer.

    Splits on sentence-ending punctuation followed by a space.
    Returns (list of sentences, remaining buffer).
    """
    sentences = []
    delimiters = [". ", "! ", "? ", ", "]

    while True:
        earliest_pos = -1
        earliest_delim = ""

        for delim in delimiters:
            pos = text.find(delim)
            if pos != -1 and (earliest_pos == -1 or pos < earliest_pos):
                earliest_pos = pos
                earliest_delim = delim

        if earliest_pos == -1:
            break

        # Include the punctuation in the sentence, not the trailing space
        sentence = text[: earliest_pos + len(earliest_delim.rstrip())]
        sentences.append(sentence)
        text = text[earliest_pos + len(earliest_delim) :]

    return sentences, text
