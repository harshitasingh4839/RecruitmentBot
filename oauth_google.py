import os
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
import httpx
import logging
logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

def _env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"{name} is not set")
    return v

def get_google_scopes() -> str:
    # Stored as space-separated in env, returned as space-separated in auth URL
    scopes = os.getenv(
        "GOOGLE_SCOPES",
        "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly"
    ).strip()
    return scopes

def build_google_auth_url(state: str) -> str:
    client_id = _env("GOOGLE_CLIENT_ID")
    redirect_uri = _env("GOOGLE_REDIRECT_URI")
    scopes = get_google_scopes()

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scopes,
        "state": state,
        "access_type": "offline",        # needed for refresh_token
        "prompt": "consent",             # ensure refresh_token is returned reliably
        "include_granted_scopes": "true"
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"

def new_state_token() -> str:
    return secrets.token_urlsafe(32)

async def exchange_code_for_tokens(code: str) -> dict:
    logger.info("Exchanging authorization code for tokens")
    client_id = _env("GOOGLE_CLIENT_ID")
    client_secret = _env("GOOGLE_CLIENT_SECRET")
    redirect_uri = _env("GOOGLE_REDIRECT_URI")

    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"
    }

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.post(GOOGLE_TOKEN_URL, data=data)
            resp.raise_for_status()
            logger.info("Token exchange successful")
            return resp.json()
        except Exception:
            logger.exception("Token exchange failed")
            raise

def utcnow():
    return datetime.now(timezone.utc)

def expires_at_from(expires_in: int | None):
    if not expires_in:
        return None
    return utcnow() + timedelta(seconds=int(expires_in))