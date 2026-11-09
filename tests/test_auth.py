"""Unit tests for dashboard authentication and the .env writer."""
import pytest

from app.config import settings
from app.core import auth
from app.core.env_file import apply_settings, update_env_file

PASSWORD = "correct-horse-battery"


def _set_password(pw=PASSWORD):
    settings.dashboard_password_hash = auth.hash_password(pw)


# ------------------------------------------------------------- .env writer

def test_update_env_file_preserves_comments_and_order(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# Twilio\nTWILIO_ACCOUNT_SID=AC1\nPORT=8000\n", encoding="utf-8")
    update_env_file({"PORT": "9000", "GEMINI_API_KEY": "abc"}, path)
    assert path.read_text(encoding="utf-8") == (
        "# Twilio\nTWILIO_ACCOUNT_SID=AC1\nPORT=9000\n\nGEMINI_API_KEY=abc\n"
    )


def test_update_env_file_quotes_special_values(tmp_path):
    path = tmp_path / ".env"
    update_env_file({"SENDGRID_FROM_EMAIL": 'Sarah "Dispatch" #1'}, path)
    assert path.read_text(encoding="utf-8") == 'SENDGRID_FROM_EMAIL="Sarah \\"Dispatch\\" #1"\n'


def test_update_env_file_rejects_newlines_and_bad_keys(tmp_path):
    path = tmp_path / ".env"
    with pytest.raises(ValueError):
        update_env_file({"GEMINI_API_KEY": "abc\nEVIL=1"}, path)
    with pytest.raises(ValueError):
        update_env_file({"bad key": "x"}, path)
    assert not path.exists()


def test_apply_settings_updates_live_settings_and_file(isolated_settings):
    apply_settings({"GEMINI_API_KEY": "g-key", "PORT": "8123"})
    assert settings.gemini_api_key == "g-key"
    assert settings.port == 8123
    assert "GEMINI_API_KEY=g-key" in isolated_settings.read_text(encoding="utf-8")


def test_apply_settings_rejects_unknown_keys(isolated_settings):
    with pytest.raises(ValueError):
        apply_settings({"NOT_A_SETTING": "x"})


# ------------------------------------------------------- hashing + tokens

def test_password_hash_roundtrip():
    encoded = auth.hash_password("s3cret-pass")
    assert encoded.startswith("pbkdf2_sha256:")
    assert "s3cret-pass" not in encoded
    assert auth.verify_password_hash("s3cret-pass", encoded)
    assert not auth.verify_password_hash("wrong", encoded)
    assert not auth.verify_password_hash("x", "garbage")


def test_check_password_supports_plain_env_password():
    settings.dashboard_password = "plain-password"
    assert auth.check_password("plain-password")
    assert not auth.check_password("nope")


def test_session_token_valid_then_expires():
    _set_password()
    token = auth.make_session_token(now=1000, ttl=60)
    assert auth.verify_session_token(token, now=1030)
    assert not auth.verify_session_token(token, now=1061)


def test_session_token_tampering_rejected():
    _set_password()
    token = auth.make_session_token()
    expires, nonce, sig = token.split(".")
    assert not auth.verify_session_token(f"{int(expires) + 999}.{nonce}.{sig}")
    assert not auth.verify_session_token("not-a-token")
    assert not auth.verify_session_token(None)


def test_password_change_invalidates_tokens():
    _set_password()
    token = auth.make_session_token()
    _set_password("a-different-password")
    assert not auth.verify_session_token(token)


def test_rate_limiter_blocks_and_expires():
    limiter = auth.LoginRateLimiter(max_failures=3, window=60)
    for t in (0, 1, 2):
        limiter.record_failure("1.2.3.4", now=100 + t)
    assert limiter.is_blocked("1.2.3.4", now=103)
    assert not limiter.is_blocked("5.6.7.8", now=103)
    assert not limiter.is_blocked("1.2.3.4", now=200)


# ------------------------------------------------- access before password

def test_local_browser_can_use_api_before_password(client):
    assert client.get("/api/stats").status_code == 200
    status = client.get("/api/auth/status").json()
    assert status == {"password_set": False, "authenticated": True, "can_create_password": True}


def test_remote_browser_blocked_before_password(remote_client):
    res = remote_client.get("/api/stats")
    assert res.status_code == 401
    assert res.json()["password_set"] is False
    assert remote_client.get("/api/auth/status").json()["can_create_password"] is False


def test_tunnelled_localhost_request_is_not_local(client):
    """ngrok forwards to 127.0.0.1 but adds X-Forwarded-For."""
    res = client.get("/api/stats", headers={"X-Forwarded-For": "198.51.100.9"})
    assert res.status_code == 401


def test_twilio_routes_stay_public(remote_client):
    _set_password()
    assert remote_client.get("/health").status_code == 200
    res = remote_client.post("/webhooks/twilio/status", data={"CallSid": "CA1", "CallStatus": "ringing"})
    assert res.status_code == 200
    assert remote_client.post("/twiml/voice").status_code == 200
    assert remote_client.get("/").status_code == 200   # the page itself shows the login screen


# --------------------------------------------------------- first password

def test_setup_password_locally(client, isolated_settings):
    res = client.post("/api/auth/setup", json={"password": PASSWORD})
    assert res.status_code == 200
    assert auth.COOKIE_NAME in res.cookies
    assert auth.verify_password_hash(PASSWORD, settings.dashboard_password_hash)
    assert "DASHBOARD_PASSWORD_HASH=pbkdf2_sha256:" in isolated_settings.read_text(encoding="utf-8")
    assert PASSWORD not in isolated_settings.read_text(encoding="utf-8")
    # Same browser is now logged in
    assert client.get("/api/stats").status_code == 200
    # A restart reads the same hash back from .env
    from app.config import Settings

    reloaded = Settings(_env_file=str(isolated_settings))
    assert reloaded.dashboard_password_hash == settings.dashboard_password_hash


def test_setup_password_rejected_remotely(remote_client):
    res = remote_client.post("/api/auth/setup", json={"password": PASSWORD})
    assert res.status_code == 403
    assert not auth.password_configured()


def test_setup_password_too_short(client):
    assert client.post("/api/auth/setup", json={"password": "short"}).status_code == 400


def test_setup_password_only_once(client):
    _set_password()
    assert client.post("/api/auth/setup", json={"password": "another-password"}).status_code == 409


# ------------------------------------------------------------ login flow

def test_password_required_even_locally_once_set(client):
    _set_password()
    assert client.get("/api/stats").status_code == 401


def test_login_logout_flow(remote_client):
    _set_password()
    assert remote_client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    res = remote_client.post("/api/auth/login", json={"password": PASSWORD})
    assert res.status_code == 200
    assert remote_client.get("/api/stats").status_code == 200
    assert remote_client.get("/api/auth/status").json()["authenticated"] is True

    remote_client.post("/api/auth/logout")
    remote_client.cookies.clear()
    assert remote_client.get("/api/stats").status_code == 401


def test_session_cookie_flags(remote_client):
    _set_password()
    res = remote_client.post("/api/auth/login", json={"password": PASSWORD},
                             headers={"X-Forwarded-Proto": "https"})
    cookie = res.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "secure" in cookie


def test_login_rate_limited(remote_client):
    _set_password()
    for _ in range(auth.login_limiter.max_failures):
        remote_client.post("/api/auth/login", json={"password": "wrong"})
    res = remote_client.post("/api/auth/login", json={"password": PASSWORD})
    assert res.status_code == 429


def test_login_without_password_configured(remote_client):
    assert remote_client.post("/api/auth/login", json={"password": "x"}).status_code == 409


def test_change_password(remote_client):
    _set_password()
    remote_client.post("/api/auth/login", json={"password": PASSWORD})
    bad = remote_client.post("/api/auth/password",
                             json={"current_password": "wrong", "new_password": "new-password-1"})
    assert bad.status_code == 401
    ok = remote_client.post("/api/auth/password",
                            json={"current_password": PASSWORD, "new_password": "new-password-1"})
    assert ok.status_code == 200
    assert auth.check_password("new-password-1")
    assert remote_client.get("/api/stats").status_code == 200   # fresh cookie issued


def test_change_password_requires_login(remote_client):
    _set_password()
    res = remote_client.post("/api/auth/password",
                             json={"current_password": PASSWORD, "new_password": "new-password-1"})
    assert res.status_code == 401
