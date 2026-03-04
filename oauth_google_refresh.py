import os
import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional
import logging
logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

def _env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"{name} is not set")
    return v

def utcnow():
    return datetime.now(timezone.utc)   # aware UTC

def to_aware_utc(dt: datetime) -> datetime:
    if dt is None:
        return dt
    # Mongo often returns naive UTC. Treat it as UTC.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    # If it is aware but not UTC, convert to UTC
    return dt.astimezone(timezone.utc)

def parse_dt(dt_val) -> Optional[datetime]:
    if not dt_val:
        return None
    if isinstance(dt_val, datetime):
        return dt_val
    # dt_val may be stored as string; parse ISO
    try:
        # Handles "2026-03-02T10:00:00+00:00" and "2026-03-02T10:00:00Z"
        s = str(dt_val).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None

def is_expired(expires_at: datetime, skew_seconds: int = 60) -> bool:
    expires_at = to_aware_utc(expires_at)
    return utcnow() >= (expires_at - timedelta(seconds=skew_seconds))

async def refresh_access_token(refresh_token: str) -> dict:
    logger.info("Refreshing Google access token")
    client_id = _env("GOOGLE_CLIENT_ID")
    client_secret = _env("GOOGLE_CLIENT_SECRET")

    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.post(GOOGLE_TOKEN_URL, data=data)
            resp.raise_for_status()
            logger.info("Token refresh successful")
            return resp.json()
        except Exception:
            logger.exception("Token refresh failed")
            raise

def compute_expires_at(expires_in: Optional[int]) -> Optional[datetime]:
    if not expires_in:
        return None
    return utcnow() + timedelta(seconds=int(expires_in))