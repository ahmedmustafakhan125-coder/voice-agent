"""Dashboard authentication: password hashing, signed session tokens,
local-request detection, and a simple login rate limiter.
"""
import hashlib
import hmac
import secrets
import threading
import time
from pathlib import Path

from fastapi import Request

COOKIE_NAME = "va_session"
SESSION_TTL = 7 * 24 * 3600
_PBKDF2_ITERATIONS = 200_000
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
# Headers added by tunnels/reverse proxies (ngrok, nginx, Cloudflare, ...)
_PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip")


# ------------------------------------------------------------------ passwords

def hash_password(password: str, salt: str | None = None,
                  iterations: int = _PBKDF2_ITERATIONS) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    # ":" separators (not "$") so the value survives .env / docker-compose
    # variable interpolation without quoting.
    return f"pbkdf2_sha256:{iterations}:{salt}:{digest.hex()}"


def verify_password_hash(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt, _ = encoded.split(":")
        if algo != "pbkdf2_sha256":
            return False
        return hmac.compare_digest(hash_password(password, salt, int(iterations)), encoded)
    except (ValueError, TypeError):
        return False


def password_configured() -> bool:
    from app.config import settings

    return bool(settings.dashboard_password_hash or settings.dashboard_password)


def check_password(password: str) -> bool:
    from app.config import settings

    if settings.dashboard_password_hash:
        return verify_password_hash(password, settings.dashboard_password_hash)
    if settings.dashboard_password:
        return hmac.compare_digest(password.encode(), settings.dashboard_password.encode())
    return False


def _password_fingerprint() -> str:
    """Changes whenever the password changes, which invalidates old sessions."""
    from app.config import settings

    return settings.dashboard_password_hash or hashlib.sha256(
        settings.dashboard_password.encode()
    ).hexdigest()


# ------------------------------------------------------------- session tokens

_secret_lock = threading.Lock()
_secret_cache: bytes | None = None


def _session_secret() -> bytes:
    """Random signing key, persisted in the data dir so sessions survive restarts."""
    global _secret_cache
    if _secret_cache is None:
        with _secret_lock:
            if _secret_cache is None:
                from app.config import settings

                path = Path(settings.data_dir) / "session_secret"
                if path.exists():
                    _secret_cache = path.read_bytes().strip()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _secret_cache = secrets.token_hex(32).encode()
                    path.write_bytes(_secret_cache)
    return _secret_cache


def reset_secret_cache() -> None:
    global _secret_cache
    _secret_cache = None


def _sign(message: str) -> str:
    key = _session_secret() + _password_fingerprint().encode()
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def make_session_token(now: float | None = None, ttl: int = SESSION_TTL) -> str:
    expires = int((now if now is not None else time.time()) + ttl)
    message = f"{expires}.{secrets.token_hex(8)}"
    return f"{message}.{_sign(message)}"


def verify_session_token(token: str | None, now: float | None = None) -> bool:
    if not token or not password_configured():
        return False
    try:
        expires, nonce, signature = token.split(".")
        if int(expires) < (now if now is not None else time.time()):
            return False
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(_sign(f"{expires}.{nonce}"), signature)


# ------------------------------------------------------------------- requests

def is_local_request(request: Request) -> bool:
    """True only for direct loopback connections (not tunnelled via ngrok etc.)."""
    host = request.client.host if request.client else ""
    if host not in _LOOPBACK:
        return False
    return not any(h in request.headers for h in _PROXY_HEADERS)


def is_https(request: Request) -> bool:
    return (request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").lower() == "https")


def is_authenticated(request: Request) -> bool:
    return verify_session_token(request.cookies.get(COOKIE_NAME))


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------- rate limiter

class LoginRateLimiter:
    """Block an IP after too many failed logins within a time window."""

    def __init__(self, max_failures: int = 10, window: float = 900):
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, ip: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(ip, []) if now - t < self.window]
        self._failures[ip] = recent
        return recent

    def is_blocked(self, ip: str, now: float | None = None) -> bool:
        with self._lock:
            return len(self._recent(ip, now or time.time())) >= self.max_failures

    def record_failure(self, ip: str, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            self._recent(ip, now).append(now)

    def reset(self, ip: str | None = None) -> None:
        with self._lock:
            if ip is None:
                self._failures.clear()
            else:
                self._failures.pop(ip, None)


login_limiter = LoginRateLimiter()
