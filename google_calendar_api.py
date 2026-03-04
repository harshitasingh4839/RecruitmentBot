import httpx
from typing import List, Dict, Any
import logging
logger = logging.getLogger(__name__)

FREEBUSY_URL = "https://www.googleapis.com/calendar/v3/freeBusy"

async def google_freebusy(access_token: str, time_min: str, time_max: str, calendar_id: str = "primary") -> List[Dict[str, str]]:
    logger.info(f"Calling Google FreeBusy API from {time_min} to {time_max}")
    headers = {"Authorization": f"Bearer {access_token}"}
    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "items": [{"id": calendar_id}]
    }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(FREEBUSY_URL, headers=headers, json=body)

        # If token is invalid/expired, Google returns 401
        if resp.status_code == 401:
            logger.warning("Google returned 401 Unauthorized")
            raise httpx.HTTPStatusError("Unauthorized", request=resp.request, response=resp)

        resp.raise_for_status()
        data: Dict[str, Any] = resp.json()

    calendars = data.get("calendars", {})
    cal = calendars.get(calendar_id, {})
    busy = cal.get("busy", [])
    logger.info(f"Received {len(busy)} busy intervals from Google")
    # busy is list of { "start": "...", "end": "..." }
    return busy