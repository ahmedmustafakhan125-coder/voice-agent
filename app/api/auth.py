"""Dashboard login API.

  GET  /auth/status     password set? logged in? allowed to create the password?
  POST /auth/setup      create the first password (local machine only)
  POST /auth/login      exchange password for a session cookie
  POST /auth/logout     clear the session cookie
  POST /auth/password   change the password (requires login)
"""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.core import auth
from app.core.env_file import apply_settings

router = APIRouter()

MIN_PASSWORD_LENGTH = 8


class PasswordBody(BaseModel):
    password: str = Field(..., max_length=256)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(..., max_length=256)
    new_password: str = Field(..., max_length=256)


def _set_session_cookie(request: Request, response: Response) -> None:
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.make_session_token(),
        max_age=auth.SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=auth.is_https(request),
        path="/",
    )


def _validate_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
        )


def _save_password(password: str) -> None:
    apply_settings({
        "DASHBOARD_PASSWORD_HASH": auth.hash_password(password),
        "DASHBOARD_PASSWORD": "",
    })


@router.get("/auth/status")
async def auth_status(request: Request):
    configured = auth.password_configured()
    local = auth.is_local_request(request)
    return {
        "password_set": configured,
        "authenticated": auth.is_authenticated(request) or (not configured and local),
        "can_create_password": not configured and local,
    }


@router.post("/auth/setup")
async def setup_password(body: PasswordBody, request: Request, response: Response):
    if auth.password_configured():
        raise HTTPException(status_code=409, detail="A password is already set")
    if not auth.is_local_request(request):
        raise HTTPException(
            status_code=403,
            detail="Create the password from the server itself (http://localhost:8000) "
                   "or set DASHBOARD_PASSWORD in .env",
        )
    _validate_new_password(body.password)
    _save_password(body.password)
    _set_session_cookie(request, response)
    return {"status": "ok"}


@router.post("/auth/login")
async def login(body: PasswordBody, request: Request, response: Response):
    ip = auth.client_ip(request)
    if auth.login_limiter.is_blocked(ip):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    if not auth.password_configured():
        raise HTTPException(status_code=409, detail="No password has been set yet")
    if not auth.check_password(body.password):
        auth.login_limiter.record_failure(ip)
        raise HTTPException(status_code=401, detail="Wrong password")
    auth.login_limiter.reset(ip)
    _set_session_cookie(request, response)
    return {"status": "ok"}


@router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.post("/auth/password")
async def change_password(body: ChangePasswordBody, request: Request, response: Response):
    if not auth.is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not logged in")
    if not auth.check_password(body.current_password):
        raise HTTPException(status_code=401, detail="Current password is wrong")
    _validate_new_password(body.new_password)
    _save_password(body.new_password)
    # Old sessions are now invalid; issue a fresh one for this browser.
    _set_session_cookie(request, response)
    return {"status": "ok"}
