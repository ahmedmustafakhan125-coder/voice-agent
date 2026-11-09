# Voice Agent — Trucking Dispatch Cold Caller

## Overview
Automated voice agent that cold-calls truckers from Google Sheets to pitch dispatching services.
Collects emails, qualifies leads, and writes results back to Google Sheets.
Target cost: **$0.02/min** per call (vs industry $0.09/min).

## Tech Stack
- **Backend**: Python 3.11+ / FastAPI / asyncio
- **STT**: Deepgram Nova-2 (streaming WebSocket, mulaw 8kHz)
- **LLM**: Google Gemini 2.0 Flash (free tier, streaming)
- **TTS**: ElevenLabs Flash v2.5 (streaming WebSocket, ulaw_8000 output)
- **Telephony**: Twilio Voice + Media Streams (client provides credentials)
- **Data**: Google Sheets (contacts + results) + SQLite call log (`data/voice_agent.db`, stdlib)
- **Email**: SendGrid

## Project Structure
```
app/
├── main.py                    # FastAPI entry point, route mounting
├── config.py                  # Pydantic settings from .env
├── pipeline/
│   ├── session.py             # CallSession — orchestrates STT→LLM→TTS per call
│   ├── stt.py                 # Deepgram streaming STT (with auto-reconnect)
│   ├── llm.py                 # Gemini streaming LLM + lead qualification
│   ├── tts.py                 # ElevenLabs streaming TTS (with auto-reconnect)
│   └── barge_in.py            # Barge-in detection + call state machine
├── telephony/
│   ├── twiml.py               # TwiML response (Connect → Stream)
│   ├── twilio_ws.py           # Twilio Media Stream WebSocket handler
│   ├── media.py               # send audio/mark/clear to Twilio (split out to avoid a circular import)
│   └── twilio_client.py       # Outbound call initiation via Twilio REST
├── automation/
│   ├── sheets.py              # Google Sheets CRUD (read contacts, write results)
│   └── email_sender.py        # SendGrid follow-up emails
├── campaign/
│   └── dialer.py              # Async rate-limited campaign dialer
├── storage/
│   └── call_log.py            # SQLite call history (powers dashboard numbers)
├── core/
│   ├── auth.py                # Password hashing, signed session cookie, rate limiter
│   ├── env_file.py            # Update .env in place + apply to live settings
│   └── integrations.py        # Setup wizard catalogue, validation, live checks
└── api/
    ├── routes.py              # REST API (campaigns, calls, stats, email)
    ├── dashboard.py           # Overview KPIs, call history, call detail
    ├── setup.py               # Setup wizard API
    ├── auth.py                # Dashboard login API
    └── webhooks.py            # Twilio status callbacks
static/index.html              # Dashboard web app (vanilla JS, no build step)
tests/                         # pytest suite (run: pytest)
```

## Running Locally
```bash
pip install -r requirements.txt
cp .env.example .env  # Fill in API keys
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Tests
```bash
pip install -r requirements-dev.txt
pytest
```
Tests never touch the real `.env` or `data/` (see `tests/conftest.py`); external APIs are mocked.

## Running with Docker
```bash
docker-compose up --build
```

## API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/campaigns` | Start a new calling campaign |
| GET | `/api/campaigns` | List all campaigns |
| GET | `/api/campaigns/{id}` | Get campaign status/stats |
| POST | `/api/campaigns/{id}/pause` | Pause a campaign |
| POST | `/api/campaigns/{id}/resume` | Resume a campaign |
| POST | `/api/campaigns/{id}/stop` | Stop a campaign |
| POST | `/api/calls/single` | Make a single test call |
| POST | `/api/email/send-followup` | Send follow-up email |
| GET | `/api/stats` | Global stats (from call log) |
| GET | `/api/dashboard/overview` | Dashboard KPIs, cost, daily trend, outcomes |
| GET | `/api/calls` / `/api/calls/{sid}` | Call history / one call with transcript |
| GET/POST | `/api/setup/...` | Setup wizard: status, save, test, credentials upload |
| GET/POST | `/api/auth/...` | Dashboard login (status, setup, login, logout, password) |
| GET | `/health` | Health check |

## Google Sheets Layout
| A: Name | B: Phone | C: Company | D: Status | E: Call SID | F: Duration | G: Interest | H: Email | I: Outcome | J: Summary | K: Called At |

Status values: `pending`, `calling`, `completed`, `failed`, `do_not_call`

## Environment Variables
See .env.example for all required keys. Clients provide their own Twilio credentials.

## Key Design Decisions
- ElevenLabs outputs ulaw_8000 directly — zero transcoding needed for Twilio
- Sentence-boundary chunking in LLM → TTS for low perceived latency
- asyncio queues decouple pipeline stages (no Celery needed at this scale)
- Google Sheets holds contacts + results; a local SQLite call log (stdlib) backs the dashboard — still no DB server
- All `/api/*` except `/api/auth/*` require dashboard login; before a password exists only direct localhost requests are allowed (proxied/ngrok traffic is not "local"). `/twiml`, `/ws`, `/webhooks`, `/health` stay public for Twilio
- Each campaign call holds its concurrency slot until its result arrives (or busy/no-answer/timeout), so results are written before the campaign finishes
- Twilio credentials are per-client config, not hardcoded
- Post-call lead qualification via Gemini extracts interest/email/outcome as structured JSON
- Auto-reconnect on STT/TTS WebSocket drops (exponential backoff, max 3 retries)
- Contact name/company flows through Twilio params → TwiML → WebSocket → CallSession → LLM prompt
- Campaign dialer uses semaphore for concurrency control + asyncio.Event for pause/resume

## Call Flow
```
1. POST /api/campaigns → CampaignDialer starts
2. Dialer reads pending contacts from Google Sheets
3. For each contact: initiate_outbound_call() → Twilio REST API
4. Twilio calls trucker, hits POST /twiml/voice → returns <Connect><Stream>
5. Twilio opens WebSocket to /ws/media-stream
6. CallSession created → greeting spoken → pipeline runs:
   Twilio audio → Deepgram STT → Gemini LLM → ElevenLabs TTS → Twilio audio
7. Call ends → post-call qualification via Gemini → results written to Sheets + call log
   (Twilio status callbacks also update the call log and release the dialer slot)
8. If qualified: follow-up email sent via SendGrid
```
