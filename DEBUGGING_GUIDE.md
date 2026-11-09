# Voice Agent — Complete Code Reference & Debugging Guide

> **Purpose:** This document explains every module, class, function, and data flow in the project so you can quickly locate and fix bugs.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Call Flow (End-to-End)](#2-call-flow-end-to-end)
3. [Module-by-Module Breakdown](#3-module-by-module-breakdown)
   - [app/main.py — Application Entry Point](#appmainpy)
   - [app/config.py — Configuration](#appconfigpy)
   - [app/pipeline/session.py — Call Orchestrator](#apppipelinesessionpy)
   - [app/pipeline/stt.py — Speech-to-Text](#apppipelinesttpy)
   - [app/pipeline/llm.py — Language Model](#apppipelinellmpy)
   - [app/pipeline/tts.py — Text-to-Speech](#apppipelinettspy)
   - [app/pipeline/barge_in.py — Interruption Detection](#apppipelinebarge_inpy)
   - [app/telephony/twiml.py — TwiML Responses](#apptelephonytwimlpy)
   - [app/telephony/twilio_ws.py — Twilio WebSocket](#apptelephonytwilio_wspy)
   - [app/telephony/twilio_client.py — Outbound Calls](#apptelephonytwilio_clientpy)
   - [app/automation/sheets.py — Google Sheets](#appautomationsheetspy)
   - [app/automation/email_sender.py — SendGrid Emails](#appautomationemail_senderpy)
   - [app/campaign/dialer.py — Campaign Engine](#appcampaigndialerpy)
   - [app/api/routes.py — REST API](#appapiroutespy)
   - [app/api/webhooks.py — Twilio Callbacks](#appapiwebhookspy)
4. [Data Flow Between Modules](#4-data-flow-between-modules)
5. [Async Queue Architecture](#5-async-queue-architecture)
6. [Common Errors & How to Fix Them](#6-common-errors--how-to-fix-them)
7. [Environment Variables Reference](#7-environment-variables-reference)
8. [Google Sheets Column Map](#8-google-sheets-column-map)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        FastAPI Server                        │
│                         (app/main.py)                        │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐  │
│  │ REST API │  │ Webhooks │  │  TwiML   │  │  WebSocket │  │
│  │ /api/*   │  │/webhooks/│  │ /twiml/* │  │  /ws/*     │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └─────┬──────┘  │
│       │              │             │               │         │
│  ┌────▼─────┐        │        ┌────▼───────────────▼──────┐  │
│  │ Campaign │        │        │      CallSession          │  │
│  │  Dialer  │        │        │  ┌─────┐ ┌─────┐ ┌─────┐ │  │
│  └────┬─────┘        │        │  │ STT │→│ LLM │→│ TTS │ │  │
│       │              │        │  └──┬──┘ └──┬──┘ └──┬──┘ │  │
│       │              │        └─────┼───────┼───────┼────┘  │
└───────┼──────────────┼──────────────┼───────┼───────┼────────┘
        │              │              │       │       │
   ┌────▼────┐   ┌─────▼────┐  ┌─────▼──┐ ┌──▼───┐ ┌─▼──────────┐
   │ Google  │   │  Twilio  │  │Deepgram│ │Gemini│ │ ElevenLabs │
   │ Sheets  │   │  (REST)  │  │  (WS)  │ │(API) │ │   (WS)     │
   └─────────┘   └──────────┘  └────────┘ └──────┘ └────────────┘
```

**6 external services:** Twilio (telephony), Deepgram (STT), Gemini (LLM), ElevenLabs (TTS), Google Sheets (data), SendGrid (email).

**Core idea:** Each phone call creates one `CallSession` that runs a real-time pipeline:
```
Caller audio → Deepgram STT → Gemini LLM → ElevenLabs TTS → Caller hears response
```

All stages are connected via **asyncio.Queue** objects so they run concurrently without blocking each other.

---

## 2. Call Flow (End-to-End)

This is the exact sequence of events from campaign start to results written:

```
Step 1: You hit POST /api/campaigns
        ↓
Step 2: CampaignDialer.start() begins
        → Reads pending contacts from Google Sheets
        → For each contact (rate-limited):
        ↓
Step 3: initiate_outbound_call() → Twilio REST API
        → Twilio dials the trucker's phone
        → Twilio hits your server at POST /twiml/voice
        ↓
Step 4: /twiml/voice returns TwiML XML:
        <Response>
          <Connect>
            <Stream url="wss://your-domain/ws/media-stream">
              <Parameter name="contact_name" value="John" />
              ...
            </Stream>
          </Connect>
        </Response>
        ↓
Step 5: Twilio opens WebSocket to /ws/media-stream
        → "start" event arrives with stream_sid, call_sid, params
        → CallSession is created
        → CallSession.run() starts in background
        ↓
Step 6: CallSession.run() does:
        a) Connect to Deepgram STT (WebSocket)
        b) Connect to ElevenLabs TTS (WebSocket)
        c) Speak greeting: "Hi {name}, this is Sarah from National Dispatch..."
        d) Start two background tasks:
           - _process_transcripts() — listens for STT events
           - _send_audio_to_caller() — sends TTS audio to Twilio
        ↓
Step 7: Real-time conversation loop:
        Twilio sends "media" events → base64 mulaw audio
        → Decoded and sent to Deepgram STT
        → Deepgram returns transcript events
        → On utterance_end: full sentence sent to Gemini LLM
        → Gemini streams response, split into sentences
        → Each sentence sent to ElevenLabs TTS
        → TTS audio chunks sent back through Twilio to caller
        ↓
Step 8: If trucker speaks while agent is talking (barge-in):
        → Deepgram fires "speech_started"
        → BargeInDetector.detect_barge_in() returns True
        → Agent audio cleared from Twilio buffer
        → LLM generation cancelled
        → System listens for new user utterance
        ↓
Step 9: Call ends (Twilio sends "stop" event):
        → session.stop() called
        → _cleanup() runs:
           - Close STT and TTS connections
           - Send full transcript to Gemini for lead qualification
           - Gemini returns JSON: {interest_level, email, outcome, summary}
           - Result pushed to CampaignDialer via on_call_complete queue
        ↓
Step 10: CampaignDialer._process_results() picks up result:
         → Writes to Google Sheets: status, duration, interest, email, outcome, summary
         → Updates campaign stats (calls_answered, qualified_leads, etc.)
```

---

## 3. Module-by-Module Breakdown

---

### `app/main.py`

**What it does:** Creates the FastAPI app, sets up logging, adds CORS middleware, and mounts all routers.

**Key details:**
- Structured logging via `structlog` — all logs use key-value format
- CORS allows ALL origins (`allow_origins=["*"]`) — fine for dev, restrict in production
- Routers mounted at:
  | Router | Prefix | Source |
  |--------|--------|--------|
  | API | `/api` | `app.api.routes` |
  | Webhooks | `/webhooks` | `app.api.webhooks` |
  | TwiML | `/twiml` | `app.telephony.twiml` |
  | WebSocket | `/ws` | `app.telephony.twilio_ws` |

**Two health endpoints:**
- `GET /` → `{"status": "ok"}`
- `GET /health` → `{"status": "healthy"}`

**When errors happen here:**
- Import errors = a module has a syntax error or missing dependency
- CORS errors in browser = check `allow_origins` setting
- Router not found = check prefix spelling matches your URL

---

### `app/config.py`

**What it does:** Loads all environment variables from `.env` into a typed `Settings` object using Pydantic Settings.

**All settings with defaults:**
| Setting | Default | Used By |
|---------|---------|---------|
| `twilio_account_sid` | `""` | twilio_client.py |
| `twilio_auth_token` | `""` | twilio_client.py |
| `twilio_phone_number` | `""` | twilio_client.py |
| `deepgram_api_key` | `""` | stt.py |
| `gemini_api_key` | `""` | llm.py |
| `elevenlabs_api_key` | `""` | tts.py |
| `elevenlabs_voice_id` | `"21m00Tcm4TlvDq8ikWAM"` (Rachel) | tts.py |
| `google_sheets_credentials_file` | `"credentials/service-account.json"` | sheets.py |
| `google_sheet_id` | `""` | sheets.py |
| `sendgrid_api_key` | `""` | email_sender.py |
| `sendgrid_from_email` | `"noreply@yourdomain.com"` | email_sender.py |
| `public_url` | `"https://your-domain.com"` | twiml.py, twilio_client.py |
| `host` | `"0.0.0.0"` | main.py |
| `port` | `8000` | main.py |

**Global instance:** `settings = Settings()` — imported everywhere as `from app.config import settings`.

**When errors happen here:**
- `ValidationError` on startup = missing required env var or wrong type
- Empty API key = the service will fail at connection time, not at config load time (because defaults are empty strings)
- `.env` file not found = all values will be empty strings

---

### `app/pipeline/session.py`

**What it does:** This is the **brain of the system**. One `CallSession` instance exists per active phone call. It orchestrates the STT → LLM → TTS pipeline and manages the entire call lifecycle.

**Class: `CallSession`**

**Constructor parameters:**
```python
CallSession(
    websocket,          # FastAPI WebSocket (to Twilio)
    stream_sid,         # Twilio's stream identifier
    call_sid,           # Twilio's call identifier
    campaign_id="",     # Which campaign this call belongs to
    contact_id="",      # Row number in Google Sheets
    agent_config_id="", # (Reserved for future agent configs)
    contact_name="",    # Trucker's name from Sheets
    contact_company=""  # Trucker's company from Sheets
)
```

**Internal state (important for debugging):**
| Attribute | Type | Purpose |
|-----------|------|---------|
| `_transcript_queue` | `asyncio.Queue` | STT events flow through here |
| `_sentence_queue` | `asyncio.Queue` | LLM sentences waiting for TTS |
| `_audio_out_queue` | `asyncio.Queue` | TTS audio chunks waiting to send to Twilio |
| `_running` | `bool` | `True` while call is active |
| `_transcript_buffer` | `str` | Accumulates partial transcripts until utterance_end |
| `_conversation_transcript` | `list[dict]` | Full conversation: `[{"role": "agent"/"user", "text": "..."}]` |
| `_start_time` | `float` | `time.time()` when call started |
| `_qualification` | `dict` | Lead qualification result (filled after call ends) |
| `on_call_complete` | `asyncio.Queue` or `None` | Set by CampaignDialer to receive results |

**Method-by-method:**

#### `async run()`
The main entry point. Called once when the call starts.
1. Connects Deepgram STT WebSocket
2. Connects ElevenLabs TTS WebSocket
3. Speaks the greeting (hardcoded: "Hi {name}, this is Sarah from National Dispatch...")
4. Spawns concurrent tasks: `_process_transcripts()` + `_send_audio_to_caller()`
5. Waits for all tasks to finish (call ended)
6. Calls `_cleanup()`

**If it crashes:** The call will go silent. Check logs for the exception.

#### `async receive_audio(audio_bytes)`
Called by `twilio_ws.py` every time Twilio sends a media frame (~20ms of mulaw audio).
- Forwards bytes directly to `self.stt.send_audio(audio_bytes)`
- If STT is disconnected, audio is silently dropped

#### `async _process_transcripts()`
Runs in a loop reading from `_transcript_queue`:
- **`speech_started`** event → checks for barge-in (if agent is speaking, triggers `_handle_barge_in()`)
- **`transcript`** event → if `is_final`, appends text to `_transcript_buffer`
- **`utterance_end`** event → takes whatever is in `_transcript_buffer`, passes it to `_generate_response()`

**Key insight:** `_transcript_buffer` accumulates multiple final transcripts until Deepgram sends `utterance_end`. This means the LLM gets the **complete user utterance**, not fragments.

#### `async _generate_response(user_text)`
1. Adds `{"role": "user", "text": user_text}` to conversation transcript
2. Sets barge-in state to PROCESSING
3. Creates a temporary sentence queue
4. Spawns `_feed_sentences_to_tts()` in parallel
5. Calls `self.llm.stream_response(user_text, sentence_queue)` — this streams Gemini's response sentence-by-sentence into the queue
6. Signals TTS feeder that streaming is done (puts `None` in queue)
7. Waits for TTS feeder to finish
8. Adds `{"role": "agent", "text": full_response}` to conversation transcript

#### `async _feed_sentences_to_tts(sentence_queue)`
Reads sentences from the queue one by one:
1. Reconnects TTS WebSocket (fresh connection per response for clean state)
2. Sets barge-in state to AGENT_SPEAKING
3. For each sentence: calls `self.tts.send_text(sentence)`
4. After all sentences: calls `self.tts.flush()` (triggers remaining audio generation)
5. Waits briefly for final audio chunks

#### `async _speak(text)`
Used for the greeting only (not for conversation responses):
1. Reconnects TTS
2. Sends text
3. Flushes
4. Waits for audio generation

#### `async _send_audio_to_caller()`
Runs continuously, reading from `_audio_out_queue`:
1. Gets audio chunk from queue
2. Calls `send_audio_to_twilio(websocket, stream_sid, audio_bytes)`
3. After each chunk, inserts a playback mark via `send_mark_to_twilio()`
4. Mark tracking used by barge-in detector to know when agent finishes speaking

#### `async _handle_barge_in()`
Called when user speaks while agent is talking:
1. Calls `clear_twilio_audio(websocket, stream_sid)` — tells Twilio to stop playing queued audio
2. Calls `self.llm.cancel()` — stops Gemini response generation
3. Drains `_sentence_queue` and `_audio_out_queue` (discards pending content)
4. Resets barge-in detector state
5. Logs "Barge-in handled"

**Result:** Agent goes silent immediately, starts listening to user's new input.

#### `async _cleanup()`
Called when the call ends:
1. Sets `_running = False`
2. Closes STT connection
3. Closes TTS connection
4. If conversation has content → runs `self.llm.qualify_lead(transcript)` for post-call analysis
5. Builds result dict: `{call_sid, campaign_id, contact_id, duration, transcript, qualification}`
6. If `on_call_complete` queue exists → pushes result dict into it (for CampaignDialer)

**When errors happen here:**
- "NoneType has no attribute" → check if STT/TTS/LLM failed to initialize
- Call goes silent → check `_running` flag, look for exceptions in `_process_transcripts` or `_send_audio_to_caller`
- Agent keeps talking after user interrupts → barge-in detection not working, check `barge_in.py`
- No results written to Sheets → check `_cleanup()` and `on_call_complete` queue

---

### `app/pipeline/stt.py`

**What it does:** Manages a persistent WebSocket connection to Deepgram for real-time speech-to-text.

**Class: `DeepgramSTT`**

**Connection URL built with these parameters:**
```
wss://api.deepgram.com/v1/listen
  ?model=nova-2
  &encoding=mulaw
  &sample_rate=8000
  &channels=1
  &punctuate=true
  &interim_results=true
  &vad_events=true
  &utterance_end_ms=1000
```

**What each parameter means:**
| Parameter | Value | Why |
|-----------|-------|-----|
| `model` | `nova-2` | Best accuracy/speed balance |
| `encoding` | `mulaw` | Twilio sends mulaw audio |
| `sample_rate` | `8000` | Phone audio is 8kHz |
| `punctuate` | `true` | Adds periods, commas |
| `interim_results` | `true` | Get partial transcripts while user is still speaking |
| `vad_events` | `true` | Get `speech_started` events (needed for barge-in) |
| `utterance_end_ms` | `1000` | Deepgram fires `utterance_end` after 1 second of silence |

**Events emitted to `on_transcript` queue:**
```python
{"type": "transcript", "text": "hello", "is_final": True, "speech_final": True}
{"type": "utterance_end"}
{"type": "speech_started"}
```

**Auto-reconnect logic:**
- On WebSocket connection error or unexpected close:
  1. Wait `RECONNECT_DELAY * (2 ** attempt)` seconds (1s, 2s, 4s)
  2. Try to reconnect (max 3 attempts)
  3. If all fail, log error and stop
- Reconnect is triggered from `send_audio()` if the connection is dead

**`close()` method:**
- Sends `{"type": "CloseStream"}` JSON to Deepgram (graceful close)
- Cancels the receive loop task
- Closes WebSocket

**When errors happen here:**
- "WebSocket connection failed" → check `DEEPGRAM_API_KEY` in `.env`
- "Reconnecting..." appears in logs → Deepgram dropped the connection (network issue or rate limit)
- Empty transcripts → check audio is actually being sent (debug `receive_audio` in session.py)
- Transcripts delayed → check `utterance_end_ms` value (1000ms = 1s silence threshold)

---

### `app/pipeline/llm.py`

**What it does:** Manages conversation with Google Gemini 2.0 Flash. Two modes: real-time streaming (during call) and post-call lead qualification.

**Class: `GeminiLLM`**

**System prompt (DEFAULT_SYSTEM_PROMPT) tells the AI to:**
- Be "Sarah from National Dispatch Services"
- Pitch dispatching services to truckers
- Ask about fleet size, truck type, current dispatch situation
- Try to collect email address
- Handle objections naturally
- Keep responses short (1-2 sentences)
- End with a question to keep conversation going

**`stream_response(user_message, sentence_queue)`:**
1. Calls Gemini's `generate_content()` with `stream=True`
2. For each chunk of text received:
   - Appends to a buffer
   - Calls `_split_sentences(buffer)` to extract complete sentences
   - Each complete sentence is immediately put into `sentence_queue`
3. After stream ends, any remaining text is also sent
4. Puts `None` into queue to signal completion

**Sentence splitting (`_split_sentences`):**
Splits on: `. ` `! ` `? ` and `, ` (when preceding text is long enough)
- This is crucial for low latency: TTS starts speaking the first sentence while Gemini is still generating the rest

**`cancel()`:**
Sets `_cancelled = True`. The `stream_response` loop checks this flag and stops if set. Used during barge-in.

**`qualify_lead(transcript)`:**
Called after the call ends:
1. Formats conversation as text: `"Agent: ... / Trucker: ..."`
2. Sends to Gemini with `QUALIFICATION_PROMPT` asking for JSON analysis
3. Parses response (handles markdown code blocks like `` ```json ... ``` ``)
4. Returns dict:
```python
{
    "interest_level": "high/medium/low/none",
    "email_collected": True/False,
    "email_address": "...",
    "company_name": "...",
    "truck_type": "...",
    "fleet_size": "...",
    "objections": ["..."],
    "outcome": "qualified/not_qualified/callback/do_not_call",
    "summary": "..."
}
```

**When errors happen here:**
- `google.api_core.exceptions.ResourceExhausted` → Gemini rate limit hit (free tier = 15 RPM)
- Empty responses → check `GEMINI_API_KEY`
- JSON parse error in `qualify_lead` → Gemini returned non-JSON; the code handles markdown blocks but edge cases exist
- Agent gives weird responses → check/edit `DEFAULT_SYSTEM_PROMPT`
- Agent responses too long → edit system prompt to emphasize brevity

---

### `app/pipeline/tts.py`

**What it does:** Streams text to ElevenLabs and receives mulaw audio back via WebSocket.

**Class: `ElevenLabsTTS`**

**Connection URL:**
```
wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input
  ?model_id=eleven_flash_v2_5
  &output_format=ulaw_8000
  &optimize_streaming_latency=3
```

**Key parameters:**
| Parameter | Value | Why |
|-----------|-------|-----|
| `model_id` | `eleven_flash_v2_5` | Fastest ElevenLabs model |
| `output_format` | `ulaw_8000` | Direct Twilio compatibility, zero transcoding |
| `optimize_streaming_latency` | `3` | Maximum latency optimization (slight quality tradeoff) |

**WebSocket protocol:**
1. **BOS (Beginning of Stream):** First message sent after connect:
   ```json
   {
     "text": " ",
     "voice_settings": {
       "stability": 0.5,
       "similarity_boost": 0.75,
       "use_speaker_boost": true
     }
   }
   ```
2. **Text chunks:** `{"text": "Hello there!", "try_trigger_generation": true}`
3. **Flush (EOS):** `{"text": ""}` — triggers generation of any remaining buffered text
4. **Response:** `{"audio": "<base64_mulaw>", "isFinal": false/true}`

**`reconnect()`:**
Closes existing connection and opens a new one. Called before each new agent response to ensure clean TTS state.

**When errors happen here:**
- "WebSocket connection failed" → check `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID`
- Choppy audio → network latency or ElevenLabs overloaded
- No audio at all → check if `send_text()` is being called (add logging)
- Audio sounds wrong → verify `ulaw_8000` format matches Twilio expectations

---

### `app/pipeline/barge_in.py`

**What it does:** Tracks who is currently speaking (user or agent) and detects when the user interrupts the agent.

**Enum `CallState`:**
```
IDLE            → Nobody speaking
USER_SPEAKING   → User is talking, agent is listening
AGENT_SPEAKING  → Agent is playing audio
PROCESSING      → System is processing (STT→LLM→TTS pipeline)
```

**Class: `BargeInDetector`**

**How mark tracking works:**
1. When agent audio is sent to Twilio, a "mark" is attached: `send_mark_to_twilio(ws, sid, "audio_1")`
2. When Twilio finishes playing that audio chunk, it sends back: `{"event": "mark", "mark": {"name": "audio_1"}}`
3. `mark_played("audio_1")` removes it from `_pending_marks`
4. When `_pending_marks` is empty and state is AGENT_SPEAKING → transition to IDLE

**Barge-in detection:**
- `detect_barge_in()` is called when Deepgram fires `speech_started`
- Returns `True` only if `state == AGENT_SPEAKING`
- Sets `_barge_in_event` (asyncio.Event) so waiting code can react

**When errors happen here:**
- Agent doesn't stop when user talks → `detect_barge_in()` might not be getting called; check if `speech_started` events arrive from Deepgram (`vad_events=true` required)
- Agent stops speaking randomly → marks might be getting lost; check Twilio WebSocket message handling
- State stuck in AGENT_SPEAKING → marks not being received; check `handle_mark()` in session.py

---

### `app/telephony/twiml.py`

**What it does:** Returns TwiML XML that tells Twilio to open a bidirectional audio stream to your WebSocket server.

**Single endpoint: `POST /twiml/voice`**

**What happens:**
1. Twilio calls this URL when a call is answered
2. Extracts query parameters: `campaign_id`, `contact_id`, `agent_config_id`, `contact_name`, `contact_company`
3. Builds WebSocket URL: replaces `https://` with `wss://` (or `http://` with `ws://`) in `PUBLIC_URL`
4. Returns XML:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://your-domain.com/ws/media-stream">
            <Parameter name="campaign_id" value="camp_abc123" />
            <Parameter name="contact_id" value="5" />
            <Parameter name="contact_name" value="John" />
            <Parameter name="contact_company" value="ABC Trucking" />
        </Stream>
    </Connect>
</Response>
```

**When errors happen here:**
- Call connects but no audio → WebSocket URL is wrong; check `PUBLIC_URL` in `.env`
- "Application error" on Twilio console → this endpoint is crashing; check FastAPI logs
- Parameters missing → check that `twilio_client.py` is passing them in the URL

---

### `app/telephony/twilio_ws.py`

**What it does:** Handles the bidirectional WebSocket connection between Twilio and your server. This is where raw phone audio enters and exits the system.

**WebSocket endpoint: `/ws/media-stream`**

**Twilio event handling:**
| Event | What Happens |
|-------|-------------|
| `"connected"` | Twilio confirms WebSocket is open. Logged only. |
| `"start"` | Stream begins. Extracts `streamSid`, `callSid`, custom parameters. Creates `CallSession`. Starts `session.run()` as background task. |
| `"media"` | Audio frame arrived. Base64-decodes the mulaw payload. Passes to `session.receive_audio()`. |
| `"mark"` | Twilio confirms it played audio up to a mark. Passes to `session.handle_mark()`. |
| `"stop"` | Stream is ending. Calls `session.stop()`. |

**Helper functions (used by CallSession):**

#### `send_audio_to_twilio(websocket, stream_sid, audio_bytes)`
Sends audio back to the caller:
```python
{
    "event": "media",
    "streamSid": "MZ...",
    "media": {"payload": "<base64_encoded_mulaw>"}
}
```

#### `send_mark_to_twilio(websocket, stream_sid, mark_name)`
Inserts a playback position marker:
```python
{
    "event": "mark",
    "streamSid": "MZ...",
    "mark": {"name": "audio_1"}
}
```

#### `clear_twilio_audio(websocket, stream_sid)`
Clears all queued audio (for barge-in):
```python
{
    "event": "clear",
    "streamSid": "MZ..."
}
```

**When errors happen here:**
- `WebSocketDisconnect` → call ended normally (Twilio closed the connection)
- "media" events not arriving → call didn't connect; check Twilio console for call status
- Audio not playing back → check `send_audio_to_twilio()` is encoding correctly
- `streamSid` is None → "start" event wasn't processed; check custom parameters extraction

---

### `app/telephony/twilio_client.py`

**What it does:** Initiates outbound phone calls via Twilio's REST API.

**Function: `initiate_outbound_call(to_number, campaign_id, contact_id, ...)`**

Step by step:
1. Creates Twilio REST client: `Client(account_sid, auth_token)`
2. Builds TwiML URL: `{PUBLIC_URL}/twiml/voice?campaign_id=X&contact_id=Y&contact_name=Z&...`
3. Builds status callback URL: `{PUBLIC_URL}/webhooks/twilio/status`
4. Calls `client.calls.create()`:
   - `to`: the trucker's phone number
   - `from_`: your Twilio phone number
   - `url`: TwiML URL (tells Twilio what to do when call connects)
   - `status_callback`: where Twilio sends call status updates
   - `status_callback_event`: `["initiated", "ringing", "answered", "completed"]`
5. Returns the `call.sid` (Twilio's unique call ID)

**When errors happen here:**
- `TwilioRestException` → bad credentials, invalid phone number, or insufficient balance
- "HTTP 401" → wrong `TWILIO_ACCOUNT_SID` or `TWILIO_AUTH_TOKEN`
- "HTTP 400" → phone number format wrong (must be E.164: `+1XXXXXXXXXX`)
- Calls not connecting → check that `PUBLIC_URL` is publicly accessible (Twilio must reach it)

---

### `app/automation/sheets.py`

**What it does:** Reads contacts from and writes results to Google Sheets.

**Class: `SheetsClient`**

**Column mapping (1-indexed):**
```
Column A (1): Name
Column B (2): Phone
Column C (3): Company
Column D (4): Status
Column E (5): Call SID
Column F (6): Duration
Column G (7): Interest
Column H (8): Email
Column I (9): Outcome
Column J (10): Summary
Column K (11): Called At
```

**Method details:**

#### `_connect()`
1. Loads service account credentials from JSON file (path from `settings.google_sheets_credentials_file`)
2. Authorizes with Google using those credentials
3. Opens spreadsheet by ID (from `settings.google_sheet_id`)
4. Gets first worksheet (sheet1)
5. Ensures headers exist (writes them if row 1 is empty)

#### `get_pending_contacts(limit=50)`
1. Fetches all values from the sheet
2. Filters rows where Status (column D) is empty `""` or `"pending"`
3. Returns list of dicts: `{"row": 5, "name": "John", "phone": "+1...", "company": "ABC"}`
4. `row` is the 1-indexed Google Sheets row number — this is used as `contact_id`

#### `mark_calling(row, call_sid)`
Updates a specific row:
- Column D → `"calling"`
- Column E → call_sid
- Column K → current timestamp

#### `write_result(row, result)`
Updates after call completion:
- Column D → `"completed"`
- Column F → duration (seconds)
- Column G → interest_level
- Column H → email_address
- Column I → outcome
- Column J → summary

#### `mark_failed(row, reason)`
- Column D → `"failed"`
- Column J → reason string

**All methods are async-wrapped:** They use `asyncio.get_event_loop().run_in_executor(None, sync_func)` because `gspread` is synchronous.

**When errors happen here:**
- `FileNotFoundError` → service account JSON file not found; check `GOOGLE_SHEETS_CREDENTIALS_FILE` path
- `gspread.exceptions.SpreadsheetNotFound` → wrong `GOOGLE_SHEET_ID`
- `gspread.exceptions.APIError 403` → service account doesn't have access to the sheet (share it with the service account email)
- `gspread.exceptions.APIError 429` → Google Sheets rate limit (100 requests per 100 seconds per user)
- Rows overwritten → check `contact_id` (row number) mapping is correct

---

### `app/automation/email_sender.py`

**What it does:** Sends follow-up emails via SendGrid to qualified leads.

**Function: `send_follow_up_email(to_email, contact_name="there")`**

1. Checks if `SENDGRID_API_KEY` is configured (returns False if not)
2. Personalizes the HTML template by replacing `{name}` with `contact_name`
3. Creates a `Mail` object with:
   - From: `settings.sendgrid_from_email`
   - To: `to_email`
   - Subject: "Great talking with you — National Dispatch info inside"
   - Both HTML and plain text versions
4. Sends via SendGrid API
5. Returns `True` if status code is 200/201/202

**Email content:**
- Thanks them for the conversation
- Lists benefits: 15-20% higher revenue, 24/7 support, load planning
- Signed as "Sarah" (matches the AI persona)

**When errors happen here:**
- `HTTP 401` → wrong `SENDGRID_API_KEY`
- `HTTP 403` → sender email not verified in SendGrid
- `HTTP 400` → invalid `to_email` format
- Returns `False` silently → check logs for the specific error

---

### `app/campaign/dialer.py`

**What it does:** Manages a calling campaign — fetches contacts, initiates calls with rate limiting, collects results, and supports pause/resume/stop.

**Dataclass: `CampaignConfig`**
```python
campaign_id: str          # Unique ID like "camp_abc12345"
max_concurrent_calls: int # Default 1 (how many calls at once)
delay_between_calls: float # Default 5.0 seconds between each new call
max_calls: int            # Default 0 = unlimited
agent_config_id: str      # Reserved for future use
```

**Dataclass: `CampaignStats`**
```python
status: str        # "idle" → "running" → "paused"/"completed"/"error"
total_contacts: int
calls_made: int
calls_answered: int
calls_failed: int
qualified_leads: int
started_at: float  # timestamp
finished_at: float # timestamp
active_calls: int  # currently in-progress
errors: list[str]
```

**Class: `CampaignDialer`**

**Global registry:**
- `_active_campaigns: dict[str, CampaignDialer]` — module-level dict
- All running campaigns registered here for lookup by API routes

#### `start()`
1. Registers self in `_active_campaigns`
2. Sets status = "running", records start time
3. Launches `_run()` as background asyncio task

#### `_run()` (main loop)
1. Fetches pending contacts from Sheets (limit = max_calls or 500)
2. Stores contacts in `_contact_map` for result lookups
3. Starts `_process_results()` in background
4. Creates `asyncio.Semaphore(max_concurrent_calls)` for rate limiting
5. For each contact:
   - Checks `_stop_event` (if stop requested, exit loop)
   - Waits on `_pause_event` (blocks while paused)
   - Acquires semaphore slot
   - Creates task for `_make_call(contact, semaphore)`
   - Sleeps `delay_between_calls` seconds
6. After all calls initiated, waits for all tasks to complete
7. Signals result processor to stop
8. Sets status = "completed"

#### `_make_call(contact, semaphore)`
1. Increments `active_calls`
2. Calls `initiate_outbound_call()` with contact data
3. On success: marks row as "calling" in Sheets, increments `calls_made`
4. On failure: marks row as "failed" in Sheets, increments `calls_failed`
5. Decrements `active_calls`, releases semaphore

**Important:** `_make_call` only **initiates** the call. It doesn't wait for the call to finish. Results come back asynchronously via `_call_results` queue.

#### `_process_results()`
Runs in a loop reading from `_call_results` queue:
1. Gets result dict from queue (put there by `CallSession._cleanup()`)
2. Looks up contact in `_contact_map` using `contact_id`
3. Calls `sheets.write_result(row, result)` to update Google Sheets
4. If `qualification.outcome == "qualified"` → increments `qualified_leads`
5. If `duration > 5` seconds → increments `calls_answered`
6. Stops when it receives `None` from queue

**How results flow from CallSession to CampaignDialer:**
```
1. CampaignDialer creates _call_results queue
2. When CallSession is created in twilio_ws.py, session.on_call_complete is set to dialer._call_results
   (Actually, this connection happens through the campaign_id param — the WS handler looks up the campaign)
3. When call ends, CallSession._cleanup() does:
   await self.on_call_complete.put(result_dict)
4. CampaignDialer._process_results() picks it up
```

**Pause/Resume mechanism:**
- `_pause_event` is an `asyncio.Event`, initially SET (= not paused)
- `pause()` → `_pause_event.clear()` → the main loop blocks at `await _pause_event.wait()`
- `resume()` → `_pause_event.set()` → the main loop continues

**When errors happen here:**
- Campaign stays "running" forever → `_run()` crashed; check logs
- Results not written to Sheets → `_process_results()` errored; check Sheets permissions
- Calls not starting → `_make_call()` failing; check Twilio credentials
- Campaign doesn't pause → `_pause_event` logic issue
- `KeyError` in `_contact_map` → `contact_id` from CallSession doesn't match row numbers from Sheets

---

### `app/api/routes.py`

**What it does:** REST API that the dashboard/client uses to control campaigns and make test calls.

**Endpoints:**

#### `POST /api/campaigns` — Start Campaign
Request body:
```json
{
    "max_concurrent_calls": 1,
    "delay_between_calls": 5.0,
    "max_calls": 0
}
```
Response:
```json
{
    "campaign_id": "camp_abc12345",
    "status": "started",
    "config": { ... }
}
```
Creates `CampaignConfig` → `CampaignDialer` → calls `dialer.start()`.

#### `GET /api/campaigns` — List All Campaigns
Returns array of all campaigns with their stats.

#### `GET /api/campaigns/{campaign_id}` — Get Campaign Status
Returns full stats for one campaign. 404 if not found.

#### `POST /api/campaigns/{id}/pause` — Pause Campaign
Calls `dialer.pause()`. Returns `{"status": "paused"}`.

#### `POST /api/campaigns/{id}/resume` — Resume Campaign
Calls `dialer.resume()`. Returns `{"status": "running"}`.

#### `POST /api/campaigns/{id}/stop` — Stop Campaign
Calls `dialer.stop()`. Returns `{"status": "stopping"}`.

#### `POST /api/calls/single` — Single Test Call
Request body:
```json
{
    "phone_number": "+1234567890",
    "contact_name": "John",
    "company": "ABC Trucking"
}
```
Calls `initiate_outbound_call()` directly (bypasses campaign system). Good for testing.

#### `POST /api/email/send-followup` — Send Email
Request body: `{"to_email": "...", "contact_name": "..."}`

#### `GET /api/stats` — Global Stats
Aggregates stats from ALL campaigns:
```json
{
    "total_campaigns": 3,
    "active_campaigns": 1,
    "total_calls": 150,
    "total_answered": 120,
    "total_qualified": 45,
    "total_failed": 30
}
```

---

### `app/api/webhooks.py`

**What it does:** Receives call status updates from Twilio.

**Endpoint: `POST /webhooks/twilio/status`**

Twilio POSTs form data whenever a call changes status:
| Field | Example |
|-------|---------|
| `CallSid` | `CA1234567890abcdef` |
| `CallStatus` | `initiated` / `ringing` / `answered` / `completed` |
| `From` | `+12025551234` |
| `To` | `+13105559876` |
| `Duration` | `45` |
| `CallDuration` | `45` |

Stores in `call_statuses` dict (in-memory, lost on restart).

**When errors happen here:**
- Not receiving callbacks → check `PUBLIC_URL` is publicly accessible
- `422 Unprocessable Entity` → Twilio sending unexpected form fields

---

## 4. Data Flow Between Modules

```
┌──────────────┐         ┌──────────────┐
│  API Routes  │────────→│   Campaign   │
│  (routes.py) │ start   │   Dialer     │
└──────────────┘         └──────┬───────┘
                                │ reads contacts
                                ▼
                         ┌──────────────┐
                         │Google Sheets  │
                         │ (sheets.py)  │
                         └──────┬───────┘
                                │ contact data
                                ▼
                         ┌──────────────┐         ┌──────────────┐
                         │Twilio Client  │────────→│   Twilio     │
                         │(twilio_client)│ REST    │   Cloud      │
                         └──────────────┘         └──────┬───────┘
                                                         │ calls phone
                                                         │ opens WebSocket
                                                         ▼
                         ┌──────────────┐         ┌──────────────┐
                         │  TwiML       │←────────│   Twilio     │
                         │ (twiml.py)   │ GET     │   Cloud      │
                         └──────────────┘         └──────┬───────┘
                                                         │ WebSocket
                                                         ▼
                         ┌──────────────────────────────────────┐
                         │         Twilio WebSocket              │
                         │        (twilio_ws.py)                 │
                         │                                       │
                         │  Creates CallSession, feeds audio     │
                         └──────────────────┬───────────────────┘
                                            │
                                            ▼
                         ┌──────────────────────────────────────┐
                         │           CallSession                 │
                         │          (session.py)                  │
                         │                                       │
                         │  ┌─────────┐  ┌─────────┐  ┌──────┐ │
                         │  │Deepgram │→ │ Gemini  │→ │Eleven│ │
                         │  │  STT    │  │  LLM    │  │ Labs │ │
                         │  │(stt.py) │  │(llm.py) │  │(tts) │ │
                         │  └─────────┘  └─────────┘  └──────┘ │
                         │                                       │
                         │  BargeInDetector (barge_in.py)        │
                         └──────────────────┬───────────────────┘
                                            │ on_call_complete
                                            ▼
                         ┌──────────────┐
                         │   Campaign   │ writes results
                         │   Dialer     │──────────────→ Google Sheets
                         └──────────────┘
```

---

## 5. Async Queue Architecture

The pipeline uses 3 async queues to decouple stages:

```
               Queue 1                Queue 2               Queue 3
Deepgram  ──→ _transcript_queue ──→  _sentence_queue  ──→  _audio_out_queue ──→ Twilio
  STT         (STT events)           (LLM sentences)       (TTS audio bytes)
              Read by:                Read by:               Read by:
              _process_transcripts()  _feed_sentences_to_tts() _send_audio_to_caller()
```

**Why queues?** Each stage runs at different speeds:
- STT: fires events every ~100ms
- LLM: generates sentences every ~200-500ms
- TTS: produces audio chunks every ~50ms

Queues let each stage work at its own pace without blocking others.

**Queue signals:**
- `None` in a queue = "no more data coming, you can stop"
- During barge-in, all queues are drained (emptied) to discard pending content

---

## 6. Common Errors & How to Fix Them

### Startup Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `ModuleNotFoundError: No module named 'X'` | Missing dependency | `pip install -r requirements.txt` |
| `pydantic.ValidationError` | Missing env var | Check `.env` file has all required vars |
| `ImportError` | Circular import or syntax error | Check the specific file mentioned in traceback |

### Connection Errors

| Error | Cause | Fix |
|-------|-------|-----|
| Deepgram "401 Unauthorized" | Bad API key | Check `DEEPGRAM_API_KEY` in `.env` |
| ElevenLabs "401" | Bad API key or voice ID | Check `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` |
| Gemini `ResourceExhausted` | Rate limit (free tier: 15 RPM) | Wait, or upgrade Gemini plan |
| Twilio `HTTP 401` | Bad account SID or auth token | Check `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` |
| Twilio `HTTP 400` | Invalid phone number | Must be E.164 format: `+1XXXXXXXXXX` |
| Google Sheets `403` | No access | Share sheet with service account email |
| Google Sheets `SpreadsheetNotFound` | Wrong sheet ID | Check `GOOGLE_SHEET_ID` |
| Google Sheets `FileNotFoundError` | Missing credentials file | Check `GOOGLE_SHEETS_CREDENTIALS_FILE` path |
| SendGrid `401` | Bad API key | Check `SENDGRID_API_KEY` |
| SendGrid `403` | Sender not verified | Verify sender email in SendGrid dashboard |

### Call Flow Errors

| Symptom | Likely Cause | Where to Look |
|---------|-------------|----------------|
| Call connects but no audio | WebSocket URL wrong | Check `PUBLIC_URL` in `.env`, check `twiml.py` URL building |
| Call connects, greeting plays, then silence | STT not connecting | Check `stt.py` logs, verify Deepgram key |
| Agent doesn't respond after user speaks | LLM not generating | Check `llm.py` logs, verify Gemini key |
| Agent responds but no audio | TTS not connecting | Check `tts.py` logs, verify ElevenLabs key |
| Agent doesn't stop when user interrupts | Barge-in broken | Check `vad_events=true` in STT config, check `barge_in.py` state |
| Agent response very slow | Sentence chunking issue | Check `_split_sentences()` in `llm.py` |
| Results not written to Sheets | Result queue disconnected | Check `on_call_complete` callback in `session.py` |
| Campaign stuck in "running" | `_run()` loop crashed | Check campaign dialer logs |
| "NoneType" errors in session | Component failed to init | Check if STT/TTS/LLM connected successfully |

### WebSocket Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `websockets.ConnectionClosed` | Deepgram/ElevenLabs dropped connection | Auto-reconnect should handle this; check logs |
| `WebSocketDisconnect` in Twilio handler | Call ended | Normal behavior, not a bug |
| "Connection refused" | Server not running or port blocked | Check uvicorn is running on correct port |

### Google Sheets Rate Limits

Google Sheets API allows ~100 requests per 100 seconds. If you're running many concurrent calls:
- Symptom: `429 Too Many Requests` errors
- Fix: Increase `delay_between_calls` in campaign config
- Long-term: Batch updates instead of individual cell writes

---

## 7. Environment Variables Reference

```bash
# === TWILIO (client provides these) ===
TWILIO_ACCOUNT_SID=ACxxxx        # From Twilio console
TWILIO_AUTH_TOKEN=xxxx            # From Twilio console
TWILIO_PHONE_NUMBER=+1xxxxxxxxxx # Your Twilio phone number

# === DEEPGRAM (STT) ===
DEEPGRAM_API_KEY=xxxx             # From Deepgram console

# === GOOGLE GEMINI (LLM) ===
GEMINI_API_KEY=xxxx               # From Google AI Studio

# === ELEVENLABS (TTS) ===
ELEVENLABS_API_KEY=xxxx           # From ElevenLabs dashboard
ELEVENLABS_VOICE_ID=xxxx          # Voice ID (default: Rachel)

# === GOOGLE SHEETS ===
GOOGLE_SHEETS_CREDENTIALS_FILE=credentials/service-account.json
GOOGLE_SHEET_ID=xxxx              # From the sheet URL

# === SENDGRID (email) ===
SENDGRID_API_KEY=xxxx
SENDGRID_FROM_EMAIL=noreply@yourdomain.com

# === SERVER ===
PUBLIC_URL=https://your-domain.com  # Must be publicly accessible for Twilio
HOST=0.0.0.0
PORT=8000
```

---

## 8. Google Sheets Column Map

| Column | Letter | Index | Field | Written By | When |
|--------|--------|-------|-------|------------|------|
| Name | A | 1 | Contact name | You (manually) | Before campaign |
| Phone | B | 2 | Phone number (E.164) | You (manually) | Before campaign |
| Company | C | 3 | Company name | You (manually) | Before campaign |
| Status | D | 4 | pending/calling/completed/failed/do_not_call | System | During campaign |
| Call SID | E | 5 | Twilio call identifier | `mark_calling()` | When call starts |
| Duration | F | 6 | Call length in seconds | `write_result()` | After call ends |
| Interest | G | 7 | high/medium/low/none | `write_result()` | After call ends |
| Email | H | 8 | Collected email address | `write_result()` | After call ends |
| Outcome | I | 9 | qualified/not_qualified/callback/do_not_call | `write_result()` | After call ends |
| Summary | J | 10 | AI-generated call summary | `write_result()` | After call ends |
| Called At | K | 11 | Timestamp | `mark_calling()` | When call starts |

**Status lifecycle:** `(empty)` or `pending` → `calling` → `completed` or `failed`

---

## Quick Debug Checklist

When something breaks, check in this order:

1. **Read the error traceback** — the file and line number tell you exactly where to look
2. **Check `.env`** — 90% of connection errors are wrong/missing API keys
3. **Check `PUBLIC_URL`** — must be publicly accessible (use ngrok for local dev)
4. **Check logs** — `structlog` outputs key-value format, search for `error` or `exception`
5. **Check Google Sheets** — look at the Status column to see where calls got stuck
6. **Check Twilio Console** — call logs show if calls connected, what TwiML was returned
7. **Check Deepgram Console** — usage logs show if STT sessions are connecting
8. **Test with single call first** — `POST /api/calls/single` bypasses campaign system
