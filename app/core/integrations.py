"""Integration catalogue for the setup wizard: fields, validation, and
live connection checks for every external service the agent uses.
"""
import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.config import settings

# Values shipped as defaults that mean "not configured yet"
PLACEHOLDERS = {
    "PUBLIC_URL": {"https://your-domain.com", ""},
    "SENDGRID_FROM_EMAIL": {"noreply@yourdomain.com", ""},
}

HTTP_TIMEOUT = 10.0


@dataclass
class Field_:
    key: str
    label: str
    secret: bool = False
    placeholder: str = ""
    help: str = ""


@dataclass
class Integration:
    id: str
    name: str
    purpose: str
    console_url: str
    fields: list[Field_]
    required: bool = True
    extra: dict = field(default_factory=dict)


INTEGRATIONS: list[Integration] = [
    Integration(
        "twilio", "Twilio", "Places the phone calls",
        "https://console.twilio.com",
        [
            Field_("TWILIO_ACCOUNT_SID", "Account SID", placeholder="ACxxxxxxxxxxxxxxxx",
                   help="Twilio Console → Account Info"),
            Field_("TWILIO_AUTH_TOKEN", "Auth Token", secret=True,
                   help="Twilio Console → Account Info"),
            Field_("TWILIO_PHONE_NUMBER", "Phone number", placeholder="+15551234567",
                   help="A Twilio number you own, in +country format"),
        ],
    ),
    Integration(
        "server", "Public URL", "Lets Twilio reach this server during calls",
        "https://ngrok.com/download",
        [
            Field_("PUBLIC_URL", "Public HTTPS URL", placeholder="https://abc123.ngrok-free.app",
                   help="Your domain, or an ngrok URL when running locally"),
        ],
    ),
    Integration(
        "deepgram", "Deepgram", "Turns the trucker's voice into text",
        "https://console.deepgram.com",
        [Field_("DEEPGRAM_API_KEY", "API key", secret=True)],
    ),
    Integration(
        "gemini", "Google Gemini", "The AI brain that writes Sarah's replies",
        "https://aistudio.google.com/app/apikey",
        [Field_("GEMINI_API_KEY", "API key", secret=True, placeholder="AIza...")],
    ),
    Integration(
        "elevenlabs", "ElevenLabs", "Gives Sarah her voice",
        "https://elevenlabs.io/app/settings/api-keys",
        [
            Field_("ELEVENLABS_API_KEY", "API key", secret=True),
            Field_("ELEVENLABS_VOICE_ID", "Voice ID", placeholder="21m00Tcm4TlvDq8ikWAM",
                   help="Voice Library → ⋯ → Copy voice ID"),
        ],
    ),
    Integration(
        "sheets", "Google Sheets", "Your contact list, and where results are written",
        "https://console.cloud.google.com/iam-admin/serviceaccounts",
        [
            Field_("GOOGLE_SHEET_ID", "Sheet link or ID",
                   placeholder="https://docs.google.com/spreadsheets/d/…",
                   help="Paste the whole link — the ID is extracted for you"),
        ],
        extra={"credentials_upload": True},
    ),
    Integration(
        "sendgrid", "SendGrid", "Sends follow-up emails to interested truckers",
        "https://app.sendgrid.com/settings/api_keys",
        [
            Field_("SENDGRID_API_KEY", "API key", secret=True, placeholder="SG...."),
            Field_("SENDGRID_FROM_EMAIL", "From email", placeholder="dispatch@yourcompany.com",
                   help="Must be a verified sender in SendGrid"),
        ],
        required=False,
    ),
]

BY_ID = {i.id: i for i in INTEGRATIONS}


# ---------------------------------------------------------------- state

def current_value(key: str) -> str:
    value = str(getattr(settings, key.lower(), "") or "")
    return "" if value in PLACEHOLDERS.get(key, set()) else value


def mask(value: str) -> str:
    if not value:
        return ""
    return "••••" + value[-4:] if len(value) > 8 else "••••"


def credentials_path() -> Path:
    return Path(settings.google_sheets_credentials_file)


def service_account_email() -> str:
    try:
        return json.loads(credentials_path().read_text(encoding="utf-8")).get("client_email", "")
    except (OSError, ValueError):
        return ""


def integration_status(integ: Integration) -> dict:
    fields = []
    for f in integ.fields:
        value = current_value(f.key)
        fields.append({
            "key": f.key,
            "label": f.label,
            "secret": f.secret,
            "placeholder": f.placeholder,
            "help": f.help,
            "set": bool(value),
            "value": mask(value) if f.secret else value,
        })
    configured = all(f["set"] for f in fields)
    status = {
        "id": integ.id,
        "name": integ.name,
        "purpose": integ.purpose,
        "console_url": integ.console_url,
        "required": integ.required,
        "fields": fields,
    }
    if integ.extra.get("credentials_upload"):
        email = service_account_email()
        status["service_account_email"] = email
        configured = configured and bool(email)
    status["configured"] = configured
    return status


def setup_overview() -> dict:
    items = [integration_status(i) for i in INTEGRATIONS]
    required = [i for i in items if i["required"]]
    public_url = current_value("PUBLIC_URL").rstrip("/")
    return {
        "integrations": items,
        "ready": all(i["configured"] for i in required),
        "configured_count": sum(i["configured"] for i in items),
        "total_count": len(items),
        "twilio_urls": {
            "voice": f"{public_url}/twiml/voice" if public_url else "",
            "status_callback": f"{public_url}/webhooks/twilio/status" if public_url else "",
        },
    }


# ----------------------------------------------------------- validation

_SHEET_URL = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SetupError(ValueError):
    pass


def normalize(key: str, value: str) -> str:
    """Clean and validate one submitted value. Raises SetupError."""
    value = (value or "").strip()
    if key == "TWILIO_ACCOUNT_SID" and not re.fullmatch(r"AC[0-9a-fA-F]{32}", value):
        raise SetupError("Account SID should start with AC followed by 32 characters")
    if key == "TWILIO_PHONE_NUMBER":
        value = re.sub(r"[\s().-]", "", value)
        if not re.fullmatch(r"\+[1-9]\d{6,14}", value):
            raise SetupError("Phone number must be in +country format, e.g. +15551234567")
    if key == "PUBLIC_URL":
        value = value.rstrip("/")
        if not re.fullmatch(r"https://[^\s/]+(/[^\s]*)?", value):
            raise SetupError("Public URL must start with https:// (Twilio requires HTTPS)")
    if key == "GOOGLE_SHEET_ID":
        m = _SHEET_URL.search(value)
        value = m.group(1) if m else value
        if not re.fullmatch(r"[a-zA-Z0-9_-]{20,}", value):
            raise SetupError("That doesn't look like a Google Sheet link or ID")
    if key == "SENDGRID_FROM_EMAIL" and not _EMAIL.match(value):
        raise SetupError("Enter a valid email address")
    if key == "ELEVENLABS_VOICE_ID" and not re.fullmatch(r"[a-zA-Z0-9]{10,40}", value):
        raise SetupError("Voice ID should be letters and numbers only")
    return value


def prepare_updates(integration_id: str, values: dict[str, str]) -> dict[str, str]:
    """Validate submitted values for one integration.

    Blank secret fields mean "keep the current value" so users never have
    to re-type keys they can't see.
    """
    integ = BY_ID.get(integration_id)
    if not integ:
        raise SetupError(f"Unknown integration: {integration_id}")
    allowed = {f.key: f for f in integ.fields}
    unknown = set(values) - set(allowed)
    if unknown:
        raise SetupError(f"Unexpected field(s): {', '.join(sorted(unknown))}")
    updates = {}
    for key, f in allowed.items():
        raw = (values.get(key) or "").strip()
        if not raw:
            if f.secret or key not in values:
                continue
            raise SetupError(f"{f.label} is required")
        updates[key] = normalize(key, raw)
    return updates


def validate_service_account(content: str) -> dict:
    try:
        data = json.loads(content)
    except ValueError:
        raise SetupError("That file isn't valid JSON")
    if not isinstance(data, dict) or data.get("type") != "service_account":
        raise SetupError("This isn't a service-account key (expected \"type\": \"service_account\")")
    for key in ("client_email", "private_key", "project_id"):
        if not data.get(key):
            raise SetupError(f"Key file is missing {key}")
    return data


def save_service_account(content: str) -> str:
    data = validate_service_account(content)
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data["client_email"]


# ------------------------------------------------------- connection checks

def http_client() -> httpx.AsyncClient:
    """Factory so tests can inject a mock transport."""
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT)


def _result(ok: bool, message: str) -> dict:
    return {"ok": ok, "message": message}


def _missing(*keys: str) -> dict | None:
    missing = [k for k in keys if not current_value(k)]
    return _result(False, f"Missing: {', '.join(missing)}") if missing else None


async def check_twilio(client: httpx.AsyncClient) -> dict:
    if err := _missing("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"):
        return err
    sid, token = settings.twilio_account_sid, settings.twilio_auth_token
    base = f"https://api.twilio.com/2010-04-01/Accounts/{sid}"
    res = await client.get(f"{base}.json", auth=(sid, token))
    if res.status_code == 401:
        return _result(False, "Twilio rejected the Account SID / Auth Token")
    if res.status_code != 200:
        return _result(False, f"Twilio returned HTTP {res.status_code}")
    account = res.json()
    nums = await client.get(f"{base}/IncomingPhoneNumbers.json",
                            params={"PhoneNumber": settings.twilio_phone_number},
                            auth=(sid, token))
    if nums.status_code == 200 and not nums.json().get("incoming_phone_numbers"):
        return _result(False, f"Connected, but {settings.twilio_phone_number} "
                              "isn't a number on this Twilio account")
    return _result(True, f"Connected to “{account.get('friendly_name', sid)}”")


async def check_server(client: httpx.AsyncClient) -> dict:
    if err := _missing("PUBLIC_URL"):
        return err
    url = f"{current_value('PUBLIC_URL')}/health"
    try:
        res = await client.get(url)
    except httpx.HTTPError:
        return _result(False, f"Couldn't reach {url} — is ngrok / your domain running?")
    if res.status_code == 200 and res.json().get("status") == "healthy":
        return _result(True, "Twilio will be able to reach this server")
    return _result(False, f"{url} answered HTTP {res.status_code}, not this server")


async def check_deepgram(client: httpx.AsyncClient) -> dict:
    if err := _missing("DEEPGRAM_API_KEY"):
        return err
    res = await client.get("https://api.deepgram.com/v1/projects",
                           headers={"Authorization": f"Token {settings.deepgram_api_key}"})
    if res.status_code == 200:
        return _result(True, "Deepgram key works")
    return _result(False, "Deepgram rejected the API key"
                   if res.status_code in (401, 403) else f"Deepgram returned HTTP {res.status_code}")


async def check_gemini(client: httpx.AsyncClient) -> dict:
    if err := _missing("GEMINI_API_KEY"):
        return err
    res = await client.get("https://generativelanguage.googleapis.com/v1beta/models",
                           headers={"x-goog-api-key": settings.gemini_api_key},
                           params={"pageSize": 1})
    if res.status_code == 200:
        return _result(True, "Gemini key works")
    return _result(False, "Google rejected the Gemini API key"
                   if res.status_code in (400, 401, 403) else f"Gemini returned HTTP {res.status_code}")


async def check_elevenlabs(client: httpx.AsyncClient) -> dict:
    if err := _missing("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"):
        return err
    res = await client.get(f"https://api.elevenlabs.io/v1/voices/{settings.elevenlabs_voice_id}",
                           headers={"xi-api-key": settings.elevenlabs_api_key})
    if res.status_code == 200:
        return _result(True, f"Voice “{res.json().get('name', 'unknown')}” is ready")
    if res.status_code in (401, 403):
        return _result(False, "ElevenLabs rejected the API key")
    if res.status_code in (400, 404):
        return _result(False, "Key works, but that Voice ID wasn't found")
    return _result(False, f"ElevenLabs returned HTTP {res.status_code}")


async def check_sheets(client: httpx.AsyncClient) -> dict:
    if err := _missing("GOOGLE_SHEET_ID"):
        return err
    if not service_account_email():
        return _result(False, "Upload your Google service-account key file first")

    def _open():
        import gspread
        from google.oauth2.service_account import Credentials

        from app.automation.sheets import SCOPES

        creds = Credentials.from_service_account_file(str(credentials_path()), scopes=SCOPES)
        sheet = gspread.authorize(creds).open_by_key(settings.google_sheet_id)
        return sheet.title, len(sheet.sheet1.get_all_values())

    try:
        title, rows = await asyncio.to_thread(_open)
    except Exception as exc:  # gspread raises many types
        text = str(exc)
        if "403" in text or "PERMISSION" in text.upper():
            return _result(False, f"No access — share the sheet with {service_account_email()}")
        if "404" in text or "not found" in text.lower():
            return _result(False, "Sheet not found — check the link")
        return _result(False, f"Couldn't open the sheet: {text[:150]}")
    contacts = max(rows - 1, 0)
    return _result(True, f"Opened “{title}” — {contacts} contact row{'s' if contacts != 1 else ''}")


async def check_sendgrid(client: httpx.AsyncClient) -> dict:
    if err := _missing("SENDGRID_API_KEY", "SENDGRID_FROM_EMAIL"):
        return err
    res = await client.get("https://api.sendgrid.com/v3/scopes",
                           headers={"Authorization": f"Bearer {settings.sendgrid_api_key}"})
    if res.status_code in (401, 403):
        return _result(False, "SendGrid rejected the API key")
    if res.status_code != 200:
        return _result(False, f"SendGrid returned HTTP {res.status_code}")
    if "mail.send" not in res.json().get("scopes", []):
        return _result(False, "Key works but lacks the “Mail Send” permission")
    return _result(True, "SendGrid can send email")


CHECKS = {
    "twilio": check_twilio,
    "server": check_server,
    "deepgram": check_deepgram,
    "gemini": check_gemini,
    "elevenlabs": check_elevenlabs,
    "sheets": check_sheets,
    "sendgrid": check_sendgrid,
}


async def run_check(integration_id: str) -> dict:
    check = CHECKS.get(integration_id)
    if not check:
        raise SetupError(f"Unknown integration: {integration_id}")
    try:
        async with http_client() as client:
            return await check(client)
    except httpx.TimeoutException:
        return _result(False, "Timed out — the service didn't answer in time")
    except httpx.HTTPError as exc:
        return _result(False, f"Network error: {exc.__class__.__name__}")
