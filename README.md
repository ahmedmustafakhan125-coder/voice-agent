<div align="center">

# 📞 Voice Agent

### An AI cold caller for trucking dispatch that runs at a fraction of the usual cost

**~$0.02 / min** target cost, against the **~$0.09 / min** industry norm: **about 78% cheaper**

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)
![Twilio](https://img.shields.io/badge/Twilio-Media_Streams-F22F46?logo=twilio&logoColor=white)
![Gemini](https://img.shields.io/badge/Gemini-2.0_Flash-8E75B2?logo=googlegemini&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)

</div>

---

## What it does

Voice Agent dials truck drivers from a Google Sheet and has a natural, real-time phone conversation with each one. The agent ("Sarah") pitches dispatching services, handles objections and collects email addresses. After each call it qualifies the lead automatically, writes the results back to the sheet and sends a follow-up email to the leads that qualify.

```
Google Sheet ─▶ Campaign Dialer ─▶ Twilio call ─▶ live AI conversation ─▶ lead scored ─▶ Sheet updated + email sent
```

## 💸 Why it's cheap

Most hosted voice-AI platforms charge around **$0.09 per minute**. This project builds its own pipeline from low-cost components and avoids extra processing steps, which brings the target down to about **$0.02 per minute**.

| Choice | Saving |
|---|---|
| **Gemini 2.0 Flash** for the LLM | Runs on the free tier, so the "brain" costs close to nothing |
| **ElevenLabs Flash v2.5 → `ulaw_8000`** | Produces audio in Twilio's own format, so there is **no transcoding** step |
| **Deepgram Nova-2 streaming** | Cheap, fast speech-to-text on raw 8 kHz μ-law audio |
| **Google Sheets as the database** | No Postgres, Redis or other paid infrastructure |
| **asyncio queues instead of Celery** | A single lightweight process, with no worker fleet or message broker |
| **Bring-your-own Twilio** | Each client pays its own telephony costs directly, with no markup |

## ⚡ Real-time pipeline

```
 Caller audio ──▶ Deepgram STT ──▶ Gemini LLM ──▶ ElevenLabs TTS ──▶ Caller audio
   (Twilio WS)     (streaming)     (streaming,      (streaming,       (Twilio WS)
                                    sentence-        μ-law 8 kHz)
                                    chunked)
```

- **Sentence-boundary chunking**: the first sentence is spoken while the LLM is still writing the rest, so the caller waits less.
- **Barge-in detection**: when the trucker interrupts, the agent stops talking, clears the queued audio and listens.
- **Auto-reconnect**: if the STT or TTS WebSockets drop, they reconnect with exponential backoff, up to 3 retries.
- **Personalised greeting**: the contact's name and company pass through Twilio → TwiML → WebSocket → the LLM prompt.
- **Post-call qualification**: Gemini pulls interest level, email, fleet size, objections, outcome and a summary from the transcript as structured JSON.

## 🧰 Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, FastAPI, asyncio |
| Speech-to-text | Deepgram Nova-2 (streaming WebSocket) |
| LLM | Google Gemini 2.0 Flash (streaming) |
| Text-to-speech | ElevenLabs Flash v2.5 (streaming WebSocket) |
| Telephony | Twilio Voice + Media Streams |
| Data | Google Sheets (gspread) |
| Email | SendGrid |
| Dashboard | Single-page HTML UI at `/` |

## 🚀 Quick start

```bash
git clone https://github.com/<your-username>/voice-agent.git
cd voice-agent
pip install -r requirements.txt
cp .env.example .env            # fill in your API keys
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Or with Docker:

```bash
docker-compose up --build
```

> Twilio has to be able to reach your server. For local development, expose port 8000 (for example with ngrok) and set `PUBLIC_URL` to the public address.

Put your Google service-account key at `credentials/service-account.json` and share the sheet with that service account.

## 📊 Google Sheet layout

| A | B | C | D | E | F | G | H | I | J | K |
|---|---|---|---|---|---|---|---|---|---|---|
| Name | Phone | Company | Status | Call SID | Duration | Interest | Email | Outcome | Summary | Called At |

`Status` values: `pending` · `calling` · `completed` · `failed` · `do_not_call`

## 🔌 API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/campaigns` | Start a calling campaign |
| `GET` | `/api/campaigns` | List campaigns |
| `GET` | `/api/campaigns/{id}` | Campaign status and stats |
| `POST` | `/api/campaigns/{id}/pause` | Pause a campaign |
| `POST` | `/api/campaigns/{id}/resume` | Resume a campaign |
| `POST` | `/api/campaigns/{id}/stop` | Stop a campaign |
| `POST` | `/api/calls/single` | Place a single test call |
| `POST` | `/api/email/send-followup` | Send a follow-up email |
| `GET` | `/api/stats` | Global stats |
| `GET` | `/health` | Health check |

## 🗂 Project structure

```
app/
├── main.py              # FastAPI entry point
├── config.py            # Settings loaded from .env
├── pipeline/            # session · stt · llm · tts · barge_in
├── telephony/           # TwiML, Twilio Media Stream WS, outbound calls
├── automation/          # Google Sheets + SendGrid
├── campaign/dialer.py   # Rate-limited async dialer (semaphore + pause/resume)
└── api/                 # REST routes + Twilio webhooks
static/index.html        # Dashboard
```

For a module-by-module walkthrough and fixes for common errors, see [DEBUGGING_GUIDE.md](DEBUGGING_GUIDE.md).

## 🔐 Configuration

All keys are listed in [`.env.example`](.env.example). Each client supplies their own Twilio credentials, and none are hardcoded.

---

<div align="center">
Built to make AI voice outreach affordable. ⭐ Star the repo if it saved you money!
</div>
