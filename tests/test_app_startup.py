"""Smoke tests: the app imports, and core routes respond."""
from fastapi.testclient import TestClient


def test_app_imports_without_circular_import():
    import app.main  # noqa: F401  — used to crash with a circular import


def test_session_uses_media_helpers():
    from app.pipeline import session
    from app.telephony import media

    assert session.send_audio_to_twilio is media.send_audio_to_twilio
    assert session.clear_twilio_audio is media.clear_twilio_audio


def test_twilio_ws_still_exports_helpers():
    from app.telephony import twilio_ws, media

    assert twilio_ws.send_mark_to_twilio is media.send_mark_to_twilio


def test_health_endpoint():
    from app.main import app

    client = TestClient(app)
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "healthy"}
