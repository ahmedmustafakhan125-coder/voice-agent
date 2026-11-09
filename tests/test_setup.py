"""Unit tests for the setup wizard API and connection checks."""
import json

import httpx
import pytest

from app.config import settings
from app.core import integrations
from app.core.integrations import SetupError, normalize, prepare_updates

SID = "AC" + "a" * 32
SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": "proj",
    "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
    "client_email": "agent@proj.iam.gserviceaccount.com",
}


@pytest.fixture
def creds_path(tmp_path):
    settings.google_sheets_credentials_file = str(tmp_path / "credentials" / "sa.json")
    return tmp_path / "credentials" / "sa.json"


@pytest.fixture
def mock_http(monkeypatch):
    """Route every outbound HTTP call in checks to a handler set by the test."""
    state = {"handler": None, "requests": []}

    def factory():
        def handle(request):
            state["requests"].append(request)
            return state["handler"](request)
        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(integrations, "http_client", factory)
    return state


# --------------------------------------------------------------- normalize

def test_normalize_phone_strips_formatting():
    assert normalize("TWILIO_PHONE_NUMBER", " +1 (555) 123-4567 ") == "+15551234567"
    with pytest.raises(SetupError):
        normalize("TWILIO_PHONE_NUMBER", "5551234567")


def test_normalize_sid():
    assert normalize("TWILIO_ACCOUNT_SID", SID) == SID
    with pytest.raises(SetupError):
        normalize("TWILIO_ACCOUNT_SID", "SK123")


def test_normalize_public_url():
    assert normalize("PUBLIC_URL", "https://abc.ngrok-free.app/") == "https://abc.ngrok-free.app"
    with pytest.raises(SetupError):
        normalize("PUBLIC_URL", "http://abc.ngrok-free.app")


def test_normalize_sheet_link_extracts_id():
    link = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0"
    assert normalize("GOOGLE_SHEET_ID", link) == SHEET_ID
    assert normalize("GOOGLE_SHEET_ID", SHEET_ID) == SHEET_ID
    with pytest.raises(SetupError):
        normalize("GOOGLE_SHEET_ID", "not a sheet")


def test_normalize_email_and_voice():
    assert normalize("SENDGRID_FROM_EMAIL", "a@b.co") == "a@b.co"
    with pytest.raises(SetupError):
        normalize("SENDGRID_FROM_EMAIL", "nope")
    with pytest.raises(SetupError):
        normalize("ELEVENLABS_VOICE_ID", "bad id!")


def test_prepare_updates_blank_secret_keeps_existing():
    updates = prepare_updates("twilio", {
        "TWILIO_ACCOUNT_SID": SID, "TWILIO_AUTH_TOKEN": "", "TWILIO_PHONE_NUMBER": "+15551234567",
    })
    assert updates == {"TWILIO_ACCOUNT_SID": SID, "TWILIO_PHONE_NUMBER": "+15551234567"}


def test_prepare_updates_rejects_foreign_fields_and_blank_required():
    with pytest.raises(SetupError):
        prepare_updates("deepgram", {"GEMINI_API_KEY": "x"})
    with pytest.raises(SetupError):
        prepare_updates("server", {"PUBLIC_URL": ""})
    with pytest.raises(SetupError):
        prepare_updates("nope", {})


# ------------------------------------------------------------------ status

def test_status_reports_unconfigured_and_masks_secrets(client, creds_path):
    settings.deepgram_api_key = "dg_secret_value_1234"
    body = client.get("/api/setup/status").json()
    items = {i["id"]: i for i in body["integrations"]}

    assert body["ready"] is False
    dg = items["deepgram"]
    assert dg["configured"] is True
    assert dg["fields"][0]["value"] == "••••1234"
    assert "dg_secret_value_1234" not in json.dumps(body)

    assert items["server"]["configured"] is False      # default placeholder URL
    assert items["sendgrid"]["required"] is False
    assert body["twilio_urls"]["voice"] == ""


def test_status_ready_when_all_required_configured(client, creds_path):
    creds_path.parent.mkdir(parents=True)
    creds_path.write_text(json.dumps(SERVICE_ACCOUNT), encoding="utf-8")
    for key, value in {
        "twilio_account_sid": SID, "twilio_auth_token": "tok", "twilio_phone_number": "+15551234567",
        "public_url": "https://x.ngrok-free.app", "deepgram_api_key": "d", "gemini_api_key": "g",
        "elevenlabs_api_key": "e", "elevenlabs_voice_id": "21m00Tcm4TlvDq8ikWAM",
        "google_sheet_id": SHEET_ID,
    }.items():
        setattr(settings, key, value)
    body = client.get("/api/setup/status").json()
    assert body["ready"] is True
    sheets = next(i for i in body["integrations"] if i["id"] == "sheets")
    assert sheets["service_account_email"] == SERVICE_ACCOUNT["client_email"]
    assert body["twilio_urls"]["status_callback"] == "https://x.ngrok-free.app/webhooks/twilio/status"


def test_setup_requires_login_when_remote(remote_client):
    assert remote_client.get("/api/setup/status").status_code == 401
    assert remote_client.post("/api/setup/deepgram", json={"values": {}}).status_code == 401


# -------------------------------------------------------------------- save

def test_save_persists_and_applies(client, isolated_settings):
    res = client.post("/api/setup/sheets", json={
        "values": {"GOOGLE_SHEET_ID": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"}
    })
    assert res.status_code == 200
    assert res.json()["updated"] == ["GOOGLE_SHEET_ID"]
    assert settings.google_sheet_id == SHEET_ID
    assert f"GOOGLE_SHEET_ID={SHEET_ID}" in isolated_settings.read_text(encoding="utf-8")


def test_save_validation_error(client):
    res = client.post("/api/setup/twilio", json={"values": {
        "TWILIO_ACCOUNT_SID": "bad", "TWILIO_PHONE_NUMBER": "+15551234567"}})
    assert res.status_code == 400
    assert "AC" in res.json()["detail"]


def test_save_unknown_integration(client):
    assert client.post("/api/setup/nope", json={"values": {}}).status_code == 404


# ------------------------------------------------------- credentials upload

def test_upload_service_account(client, creds_path):
    res = client.post("/api/setup/sheets/credentials", json={"content": json.dumps(SERVICE_ACCOUNT)})
    assert res.status_code == 200
    assert res.json()["service_account_email"] == SERVICE_ACCOUNT["client_email"]
    assert json.loads(creds_path.read_text(encoding="utf-8"))["client_email"] == SERVICE_ACCOUNT["client_email"]


@pytest.mark.parametrize("content", [
    "not json",
    json.dumps({"type": "authorized_user"}),
    json.dumps({**SERVICE_ACCOUNT, "private_key": ""}),
])
def test_upload_rejects_bad_files(client, creds_path, content):
    assert client.post("/api/setup/sheets/credentials", json={"content": content}).status_code == 400
    assert not creds_path.exists()


# --------------------------------------------------------- connection tests

def test_check_reports_missing_values(client):
    body = client.post("/api/setup/test/deepgram").json()
    assert body == {"ok": False, "message": "Missing: DEEPGRAM_API_KEY"}


def test_check_unknown_integration(client):
    assert client.post("/api/setup/test/nope").status_code == 404


def test_twilio_check_success(client, mock_http):
    settings.twilio_account_sid, settings.twilio_auth_token = SID, "tok"
    settings.twilio_phone_number = "+15551234567"

    def handler(req):
        if req.url.path.endswith("IncomingPhoneNumbers.json"):
            assert req.url.params["PhoneNumber"] == "+15551234567"
            return httpx.Response(200, json={"incoming_phone_numbers": [{"sid": "PN1"}]})
        assert req.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, json={"friendly_name": "Dispatch Co"})

    mock_http["handler"] = handler
    body = client.post("/api/setup/test/twilio").json()
    assert body["ok"] is True and "Dispatch Co" in body["message"]


def test_twilio_check_bad_credentials_and_foreign_number(client, mock_http):
    settings.twilio_account_sid, settings.twilio_auth_token = SID, "tok"
    settings.twilio_phone_number = "+15551234567"
    mock_http["handler"] = lambda req: httpx.Response(401)
    assert "rejected" in client.post("/api/setup/test/twilio").json()["message"]

    mock_http["handler"] = lambda req: httpx.Response(
        200, json={"incoming_phone_numbers": []} if "Incoming" in req.url.path else {})
    body = client.post("/api/setup/test/twilio").json()
    assert body["ok"] is False and "isn't a number" in body["message"]


@pytest.mark.parametrize("integration,attrs,ok_status,ok_json,auth_header", [
    ("deepgram", {"deepgram_api_key": "dg"}, 200, {}, ("authorization", "Token dg")),
    ("gemini", {"gemini_api_key": "gm"}, 200, {"models": []}, ("x-goog-api-key", "gm")),
    ("elevenlabs", {"elevenlabs_api_key": "el", "elevenlabs_voice_id": "21m00Tcm4TlvDq8ikWAM"},
     200, {"name": "Rachel"}, ("xi-api-key", "el")),
    ("sendgrid", {"sendgrid_api_key": "SG.x", "sendgrid_from_email": "a@b.co"},
     200, {"scopes": ["mail.send"]}, ("authorization", "Bearer SG.x")),
])
def test_api_key_checks(client, mock_http, integration, attrs, ok_status, ok_json, auth_header):
    for k, v in attrs.items():
        setattr(settings, k, v)

    def ok(req):
        assert req.headers[auth_header[0]] == auth_header[1]
        return httpx.Response(ok_status, json=ok_json)

    mock_http["handler"] = ok
    assert client.post(f"/api/setup/test/{integration}").json()["ok"] is True

    mock_http["handler"] = lambda req: httpx.Response(401)
    body = client.post(f"/api/setup/test/{integration}").json()
    assert body["ok"] is False and "rejected" in body["message"]


def test_elevenlabs_unknown_voice(client, mock_http):
    settings.elevenlabs_api_key, settings.elevenlabs_voice_id = "el", "doesNotExist123"
    mock_http["handler"] = lambda req: httpx.Response(404)
    assert "Voice ID" in client.post("/api/setup/test/elevenlabs").json()["message"]


def test_sendgrid_key_without_mail_send(client, mock_http):
    settings.sendgrid_api_key, settings.sendgrid_from_email = "SG.x", "a@b.co"
    mock_http["handler"] = lambda req: httpx.Response(200, json={"scopes": ["stats.read"]})
    assert "Mail Send" in client.post("/api/setup/test/sendgrid").json()["message"]


def test_server_check(client, mock_http):
    settings.public_url = "https://x.ngrok-free.app"
    mock_http["handler"] = lambda req: httpx.Response(200, json={"status": "healthy"})
    assert client.post("/api/setup/test/server").json()["ok"] is True

    mock_http["handler"] = lambda req: httpx.Response(404, text="ngrok offline")
    assert client.post("/api/setup/test/server").json()["ok"] is False

    def boom(req):
        raise httpx.ConnectError("refused", request=req)

    mock_http["handler"] = boom
    assert "Couldn't reach" in client.post("/api/setup/test/server").json()["message"]


def test_timeout_is_reported_nicely(client, mock_http):
    settings.deepgram_api_key = "dg"

    def slow(req):
        raise httpx.ReadTimeout("slow", request=req)

    mock_http["handler"] = slow
    assert client.post("/api/setup/test/deepgram").json() == {
        "ok": False, "message": "Timed out — the service didn't answer in time"}


def test_sheets_check_needs_credentials(client, creds_path):
    settings.google_sheet_id = SHEET_ID
    assert "service-account" in client.post("/api/setup/test/sheets").json()["message"]
