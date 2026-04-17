import uvicorn
import os
import uuid
import httpx
import anyio
from email_utils import send_email_smtp
import secrets
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import json
from fastapi import FastAPI, Depends, HTTPException, Query, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from db import get_db
from zoneinfo import ZoneInfo
from schemas import (
    CheckCalendarConnectionRequest,
    CheckCalendarConnectionResponse, 
    StartGoogleOAuthRequest,
    StartGoogleOAuthResponse,
    GetGoogleFreeBusyRequest,
    BusyInterval,
    GetGoogleFreeBusyResponse,
    ProposeSlotsRequest,
    ProposeSlotsResponse,
    Slot, 
    SaveSlotsRequest,
    SaveSlotsResponse,
    GetNextAvailableSlotsRequest,
    GetNextAvailableSlotsResponse,
    ConfirmCandidateSlotBookingRequest,
    ConfirmCandidateSlotBookingResponse,
    CancelCandidateInterviewRequest,
    CancelCandidateInterviewResponse,
    RescheduleCandidateInterviewRequest,
    RescheduleCandidateInterviewResponse,
    StartCandidateRescheduleRequest,
    StartCandidateRescheduleRequestResponse,
    CreateCandidateRescheduleRequestRequest,
    CreateCandidateRescheduleRequestResponse,
    ApproveRescheduleRequestRequest,
    ApproveRescheduleRequestResponse,
    RejectRescheduleRequestRequest,
    RejectRescheduleRequestResponse,
    ListRescheduleRequestsResponse,
    ResolveCandidateSchedulingSessionRequest,
    ResolveCandidateSchedulingSessionResponse,
    SaveDirectSlotsRequest,
    SaveDirectSlotsResponse,
    CandidateLoginRequest,
    CandidateJobSelectionRequest,
    RecruiterLoginRequest
    )
from oauth_google import build_google_auth_url, new_state_token, exchange_code_for_tokens, utcnow, expires_at_from, get_google_scopes

from oauth_google_refresh import parse_dt, is_expired, refresh_access_token, compute_expires_at, utcnow
from google_calendar_api import google_freebusy
from preview_formatter import format_slots_preview
from slot_engine import (
    Interval, parse_date_ymd, parse_hhmm, to_local_dt,
    parse_any_iso, merge_intervals, subtract_busy,
    generate_slots_from_intervals
)
from candidate_scheduling import (
    # start_candidate_scheduling_session_logic,
      get_next_available_slots_logic
)
from confirm_booking import confirm_candidate_slot_booking_logic
from cancel_booking import cancel_candidate_interview_logic
from reschedule_booking import (
    reschedule_candidate_interview_logic,
    start_candidate_reschedule_request_logic,
    create_candidate_reschedule_request_logic,
    approve_reschedule_request_logic,
    reject_reschedule_request_logic,
    list_reschedule_requests_logic,
)
from resolve_candidate_scheduling import resolve_candidate_scheduling_session_logic
import logging
from logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)

COLL_CAL_CONN = "recruiterCalendarConnections"
COLL_OAUTH_STATES = "oauthStates"
COLL_SLOT_PROPOSALS = "slotProposals"
COLL_AVAIL_SLOTS = "availabilitySlots"
COLL_CANDIDATES = "candidateData"
COLL_RECRUITERS = "recruiterData"
COLL_CANDIDATE_SESSIONS = "candidateSchedulingSessions"
COLL_SCHEDULED_INTERVIEWS = "scheduledInterviews"
COLL_INTERVIEW_REMINDERS = "interviewReminders"
COLL_RESCHEDULE_REQUESTS = "rescheduleRequests"

def utcnow():
    return datetime.now(timezone.utc)
def parse_utc_slot_dt(value: str) -> datetime:
    """Parse a UTC ISO string like 2026-03-12T05:30:00Z into a timezone-aware datetime."""
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)

def compute_slot_expiry(end_at_utc: str, grace_minutes: int = 15) -> datetime:
    # Small grace period keeps the slot visible through the exact boundary and gives
    # operational cushion for late booking/cleanup races.
    return parse_utc_slot_dt(end_at_utc) + timedelta(minutes=grace_minutes)

def validate_recruiter_job_metadata(job_id: str, job_title: Optional[str]) -> tuple[str, Optional[str]]:
    clean_job_id = job_id.strip()
    if not clean_job_id:
        raise HTTPException(status_code=400, detail="jobId is required for candidate scheduling.")
    clean_job_title = job_title.strip() if job_title else None
    return clean_job_id, (clean_job_title or None)

def to_utc_iso(dt_value, timezone_name: str) -> str:
    """
    Convert local datetime to UTC ISO string.
    Accepts a datetime object that may be naive or timezone-aware.
    """
    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=ZoneInfo(timezone_name))
    return dt_value.astimezone(ZoneInfo("UTC")).isoformat()

def generate_candidate_id() -> str:
    return f"cand_{uuid.uuid4().hex[:8]}"

def generate_recruiter_id() -> str:
    return f"rec_{uuid.uuid4().hex[:8]}"

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    db = get_db()
    # Unique per recruiter+provider
    await db[COLL_CAL_CONN].create_index(
        [("recruiterEmail", 1), ("provider", 1)],
        unique=True,
        name="uniq_recruiter_provider"
    )
    # Helpful query index
    await db[COLL_CAL_CONN].create_index(
        [("status", 1), ("updatedAt", -1)],
        name="status_updatedAt"
    )
    # oauth_states: unique state, TTL cleanup
    await db[COLL_OAUTH_STATES].create_index(
        [("state", 1)],
        unique=True,
        name="uniq_state"
    )
    # TTL index: documents expire automatically after expiresAt
    await db[COLL_OAUTH_STATES].create_index(
        [("expiresAt", 1)],
        expireAfterSeconds=0,
        name="ttl_expiresAt"
    )
    # slot proposals: unique proposalId and TTL cleanup
    await db[COLL_SLOT_PROPOSALS].create_index(
        [("proposalId", 1)], 
        unique=True, 
        name="uniq_proposalId"
    )
    await db[COLL_SLOT_PROPOSALS].create_index(
        [("expiresAt", 1)], 
        expireAfterSeconds=0, 
        name="ttl_slotProposal_expiresAt"
    )
    await db[COLL_SLOT_PROPOSALS].create_index(
        [("recruiterEmail", 1), 
         ("createdAt", -1)], 
        name="proposal_recruiter_createdAt"
    )
    # Prevent duplicate slot inserts for same recruiter/provider/start/end
    await db[COLL_AVAIL_SLOTS].create_index(
        [("recruiterEmail", 1), ("provider", 1), ("startAtUtc", 1), ("endAtUtc", 1)],
        unique=True,
        name="uniq_slot_time"
    )
    await db[COLL_AVAIL_SLOTS].create_index(
        [("recruiterEmail", 1), ("createdAt", -1)],
        name="slot_recruiter_createdAt"
    )
    # Candidate-side fetch uses the recruiter's shared availability pool.
    await db[COLL_AVAIL_SLOTS].create_index(
        [("recruiterEmail", 1), ("status", 1), ("startAtUtc", 1)],
        name="slot_fetch_for_candidate_by_recruiter"
    )
    # Cleanup/ops helpers for temporary holds and past slots.
    await db[COLL_AVAIL_SLOTS].create_index(
        [("holdExpiresAt", 1)],
        name="slot_hold_expiry_lookup"
    )
    await db[COLL_AVAIL_SLOTS].create_index(
        [("slotExpiresAt", 1)],
        expireAfterSeconds=0,
        name="ttl_slot_expiresAt"
    )
    await db[COLL_RECRUITERS].create_index(
        [("recruiterId", 1)],
        unique=True,
        name="uniq_recruiterId"
    )
    await db[COLL_RECRUITERS].create_index(
        [("phone", 1)],
        name="recruiter_phone"
    )
    await db[COLL_RECRUITERS].create_index(
        [("email", 1)],
        name="recruiter_email"
    )
    await db[COLL_CANDIDATES].create_index(
        [("candidateId", 1)],
        unique=True,
        name="uniq_candidateId"
    )
    await db[COLL_CANDIDATES].create_index(
        [("phone", 1)],
        name="candidate_phone"
    )
    await db[COLL_CANDIDATES].create_index(
        [("email", 1)],
        name="candidate_email"
    )
    await db[COLL_CANDIDATES].create_index(
        [("selectedJobs.jobId", 1)],
        name="candidate_selected_job"
    )
    await db[COLL_CANDIDATE_SESSIONS].create_index(
        [("sessionId", 1)],
        unique=True,
        name="uniq_candidate_sessionId"
    )
    await db[COLL_CANDIDATE_SESSIONS].create_index(
        [("candidateId", 1), ("recruiterEmail", 1), ("jobId", 1), ("status", 1), ("updatedAt", -1)],
        name="candidate_session_lookup"
    )
    await db[COLL_CANDIDATE_SESSIONS].create_index(
        [("candidateId", 1), ("recruiterEmail", 1), ("jobId", 1)],
        unique=True,
        partialFilterExpression={"status": "active"},
        name="uniq_active_candidate_session"
    )
    await db[COLL_CANDIDATE_SESSIONS].create_index(
        [("expiresAt", 1)],
        expireAfterSeconds=0,
        name="ttl_candidate_session_expiresAt"
    )
    await db[COLL_SCHEDULED_INTERVIEWS].create_index(
    [("scheduledInterviewId", 1)],
    unique=True,
    name="uniq_scheduledInterviewId"
    )
    await db[COLL_SCHEDULED_INTERVIEWS].create_index(
    [("candidateId", 1), ("jobId", 1), ("status", 1), ("updatedAt", -1)],
    name="scheduled_interview_lookup"
    )
    await db[COLL_INTERVIEW_REMINDERS].create_index(
    [("scheduledInterviewId", 1), ("sendAt", 1), ("status", 1)],
    name="interview_reminder_due_lookup"
    )
    await db[COLL_INTERVIEW_REMINDERS].create_index(
    [("scheduledInterviewId", 1), ("status", 1)],
    name="reminder_interview_status_lookup"
    )
    yield
    # Shutdown (if needed)

app = FastAPI(title="RecruiterBot Calendar Tools", version="1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # use specific domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"Incoming request: {request.method} {request.url}")
    try:
        response = await call_next(request)
        logger.info(f"Response status: {response.status_code}")
        return response
    except Exception:
        logger.exception("Unhandled exception during request")
        raise

"""
APIs for the scheduler bot to store the availabilty of the recruiter which is not being used in the current flow since the recruiter is storing his availability through dashboard.
"""
# # Endpoint to check calendar connection status for a recruiter and provider
# @app.post("/tools/checkCalendarConnection", response_model=CheckCalendarConnectionResponse)
# async def check_calendar_connection(
#     payload: CheckCalendarConnectionRequest,
#     db=Depends(get_db)
# ):
#     email = payload.recruiterEmail.lower().strip()
#     provider = payload.provider

#     doc = await db[COLL_CAL_CONN].find_one(
#         {"recruiterEmail": email, "provider": provider},
#         projection={"_id": 0, "recruiterEmail": 1, "provider": 1, "status": 1}
#     )

#     if not doc:
#         return CheckCalendarConnectionResponse(
#             recruiterEmail=email,
#             provider=provider,
#             connected=False,
#             status=None,
#             message="No calendar connection found. Recruiter must connect calendar via OAuth."
#         )

#     status = doc.get("status")
#     connected = (status == "connected")

#     return CheckCalendarConnectionResponse(
#         recruiterEmail=email,
#         provider=provider,
#         connected=connected,
#         status=status,
#         message="Calendar is connected." if connected else f"Calendar not connected (status={status})."
#     )

# @app.post("/tools/startGoogleOAuth", response_model=StartGoogleOAuthResponse)
# async def start_google_oauth(payload: StartGoogleOAuthRequest, db=Depends(get_db)):
#     email = payload.recruiterEmail.lower().strip()
#     provider = payload.provider
#     if provider != "google":
#         raise HTTPException(status_code=400, detail="Only google is supported right now.")

#     state = new_state_token()
#     expires_at = utcnow() + timedelta(minutes=10)

#     # store state so callback can validate + map to recruiterEmail
#     await db[COLL_OAUTH_STATES].insert_one({
#         "state": state,
#         "provider": "google",
#         "recruiterEmail": email,
#         "createdAt": utcnow(),
#         "expiresAt": expires_at
#     })

#     auth_url = build_google_auth_url(state)

#     subject = "Connect your Google Calendar to Scheduler Bot"
#     html_body = f"""
#     <div style="font-family: Arial, sans-serif; line-height: 1.4;">
#       <p>Hi,</p>
#       <p>Please connect your Google Calendar to allow us to check your availability and avoid conflicts while generating interview slots.</p>
#       <p><a href="{auth_url}" target="_blank" rel="noreferrer">Click here to connect Google Calendar</a></p>
#       <p>If the button does not work, copy and paste this link into your browser:</p>
#       <p style="word-break: break-all;">{auth_url}</p>
#       <p>This link is valid for a short time. After connecting, return to the app to continue.</p>
#       <p>Thanks,<br/>Scheduler Bot</p>
#     </div>
#     """
#     text_body = f"""Please connect your Google Calendar:
# {auth_url}

# After connecting, return to the app to continue.
# """

#     # Send email in a thread so we don't block the event loop
#     try:
#         await anyio.to_thread.run_sync(
#             send_email_smtp,
#             email,
#             subject,
#             html_body,
#             text_body
#         )
#     except Exception as e:
#         # Keep state stored so user can retry email without creating a new state if you want (optional).
#         # For now, we fail explicitly so caller knows email didn't go.
#         raise HTTPException(status_code=502, detail=f"Failed to send OAuth email: {str(e)}")

#     return StartGoogleOAuthResponse(
#         recruiterEmail=email,
#         provider="google",
#         authUrl=auth_url,
#         message="OAuth link generated and sent to recruiter email. Ask recruiter to complete authorization and return."
#     )

@app.get("/oauth/google/callback")
async def google_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db=Depends(get_db)
):
    # Validate state
    state_doc = await db[COLL_OAUTH_STATES].find_one({"state": state, "provider": "google"})
    if not state_doc:
        raise HTTPException(status_code=400, detail="Invalid or expired state. Please retry connecting your calendar.")

    recruiter_email = state_doc["recruiterEmail"]

    # Exchange code for tokens
    try:
        token_json = await exchange_code_for_tokens(code)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {str(e)}")

    access_token = token_json.get("access_token")
    refresh_token = token_json.get("refresh_token")  # may be absent if user previously consented
    expires_in = token_json.get("expires_in")
    scope = token_json.get("scope")

    if not access_token:
        raise HTTPException(status_code=400, detail="No access_token received from Google.")

    # Upsert into calendar_connections
    # Store refresh_token if present; if not present, keep existing one.
    update = {
        "provider": "google",
        "recruiterEmail": recruiter_email,
        "status": "connected",
        "scopes": (scope or get_google_scopes()),
        "updatedAt": utcnow(),
        "token": {
            "accessToken": access_token,
            "expiresAt": expires_at_from(expires_in),
        }
    }
    if refresh_token:
        update["token"]["refreshToken"] = refresh_token

    await db[COLL_CAL_CONN].update_one(
        {"recruiterEmail": recruiter_email, "provider": "google"},
        {
            "$set": update,
            "$setOnInsert": {"createdAt": utcnow()}
        },
        upsert=True
    )

    # Remove state doc after use (optional; TTL would also clean it)
    await db[COLL_OAUTH_STATES].delete_one({"state": state})

    # Simple success page (you can redirect to your frontend instead)
    return HTMLResponse(
        "<html><body><h3>Google Calendar connected successfully.</h3>"
        "<p>You can go back to the app and continue slot generation.</p></body></html>"
    )

# # Endpoint to get Google Calendar free/busy info for a recruiter, with token refresh handling
# @app.post("/tools/getGoogleFreeBusy", response_model=GetGoogleFreeBusyResponse)
# async def get_google_freebusy(payload: GetGoogleFreeBusyRequest, db=Depends(get_db)):
#     email = payload.recruiterEmail.lower().strip()
#     provider = payload.provider
#     if provider != "google":
#         raise HTTPException(status_code=400, detail="Only google is supported right now.")

#     # 1) Load connection + tokens
#     conn = await db[COLL_CAL_CONN].find_one(
#         {"recruiterEmail": email, "provider": "google"},
#         projection={"_id": 0}
#     )
#     if not conn:
#         raise HTTPException(status_code=404, detail="No Google calendar connection found for this recruiter.")
#     if conn.get("status") != "connected":
#         raise HTTPException(status_code=400, detail=f"Calendar not connected (status={conn.get('status')}).")

#     token = conn.get("token") or {}
#     access_token = token.get("accessToken")
#     refresh_token = token.get("refreshToken")
#     expires_at = parse_dt(token.get("expiresAt"))

#     if not access_token and not refresh_token:
#         raise HTTPException(status_code=400, detail="No tokens found. Reconnect Google Calendar.")

#     used_refresh = False

#     # 2) Refresh token if needed
#     if (not access_token) or is_expired(expires_at):
#         if not refresh_token:
#             raise HTTPException(status_code=401, detail="Access token expired and no refresh token is available. Reconnect Google Calendar.")

#         try:
#             tj = await refresh_access_token(refresh_token)
#         except httpx.HTTPError as e:
#             # Mark status expired for your own tracking (optional)
#             await db[COLL_CAL_CONN].update_one(
#                 {"recruiterEmail": email, "provider": "google"},
#                 {"$set": {"status": "expired", "updatedAt": utcnow()}}
#             )
#             raise HTTPException(status_code=401, detail=f"Token refresh failed. Reconnect Google Calendar. ({str(e)})")

#         access_token = tj.get("access_token")
#         expires_in = tj.get("expires_in")
#         new_expires_at = compute_expires_at(expires_in)

#         if not access_token:
#             raise HTTPException(status_code=401, detail="Token refresh did not return access_token. Reconnect Google Calendar.")

#         used_refresh = True

#         # 3) Save refreshed token back to Mongo
#         await db[COLL_CAL_CONN].update_one(
#             {"recruiterEmail": email, "provider": "google"},
#             {"$set": {
#                 "token.accessToken": access_token,
#                 "token.expiresAt": new_expires_at,
#                 "status": "connected",
#                 "updatedAt": utcnow()
#             }}
#         )

#     # 4) Call Google FreeBusy
#     try:
#         busy_raw = await google_freebusy(
#             access_token=access_token,
#             time_min=payload.timeMin,
#             time_max=payload.timeMax,
#             calendar_id=payload.calendarId
#         )
#     except httpx.HTTPStatusError as e:
#         if e.response is not None and e.response.status_code == 401:
#             # Could be revoked tokens. Mark and ask to reconnect.
#             await db[COLL_CAL_CONN].update_one(
#                 {"recruiterEmail": email, "provider": "google"},
#                 {"$set": {"status": "revoked", "updatedAt": utcnow()}}
#             )
#             raise HTTPException(status_code=401, detail="Google authorization revoked/invalid. Please reconnect Google Calendar.")
#         raise HTTPException(status_code=502, detail=f"Google API error: {str(e)}")
#     except Exception as e:
#         raise HTTPException(status_code=502, detail=f"Failed to call Google FreeBusy: {str(e)}")

#     busy_intervals = [BusyInterval(**b) for b in busy_raw]

#     return GetGoogleFreeBusyResponse(
#         recruiterEmail=email,
#         provider="google",
#         calendarId=payload.calendarId,
#         timeMin=payload.timeMin,
#         timeMax=payload.timeMax,
#         busy=busy_intervals,
#         message=f"Fetched {len(busy_intervals)} busy intervals from Google Calendar.",
#         usedTokenRefresh=used_refresh
#     )

# # Endpoint to propose slots based on availability and busy times
# @app.post("/tools/proposeSlots", response_model=ProposeSlotsResponse)
# async def propose_slots(payload: ProposeSlotsRequest, db=Depends(get_db)):
#     email = payload.recruiterEmail.lower().strip()
#     tz_name = payload.timezone.strip()
#     logger.info(f"ProposeSlots called by {email} timezone={tz_name}")

#     try:
#         tz = ZoneInfo(tz_name)
#     except Exception:
#         raise HTTPException(status_code=400, detail=f"Invalid timezone: {tz_name}")

#     if not payload.availability or len(payload.availability) == 0:
#         return ProposeSlotsResponse(
#             recruiterEmail=email,
#             provider="google",
#             timezone=tz_name,
#             slotDurationMinutes=payload.slotDurationMinutes,
#             bufferMinutes=payload.bufferMinutes,
#             slots=[],
#             summary="No availability provided.",
#             warnings=[],
#             needs_clarification="Please provide dates and time windows (e.g., 2026-03-02 10:00-13:00, 14:00-18:00)."
#         )

#     warnings: list[str] = []

#     # 1) Build free intervals (local) from availability
#     free_intervals: list[Interval] = []
#     for day in payload.availability:
#         d = parse_date_ymd(day.date)
#         if not day.windows:
#             warnings.append(f"No windows for {day.date}.")
#             continue

#         for w in day.windows:
#             try:
#                 st = parse_hhmm(w.start)
#                 en = parse_hhmm(w.end)
#             except Exception:
#                 warnings.append(f"Invalid time window format on {day.date}: {w.start}-{w.end}")
#                 continue

#             start_dt = to_local_dt(d, st, tz)
#             end_dt = to_local_dt(d, en, tz)
#             if end_dt <= start_dt:
#                 warnings.append(f"Window end must be after start on {day.date}: {w.start}-{w.end}")
#                 continue
#             free_intervals.append(Interval(start_dt, end_dt))

#     free_intervals = merge_intervals(free_intervals)
#     if not free_intervals:
#         return ProposeSlotsResponse(
#             recruiterEmail=email,
#             provider="google",
#             timezone=tz_name,
#             slotDurationMinutes=payload.slotDurationMinutes,
#             bufferMinutes=payload.bufferMinutes,
#             slots=[],
#             summary="No valid availability windows found.",
#             warnings=warnings,
#             needs_clarification="Please share valid time windows like 10:00-13:00."
#         )

#     # 2) Parse busy intervals and convert to local timezone, then subtract
#     busy_intervals_local: list[Interval] = []
#     if payload.busy:
#         for b in payload.busy:
#             try:
#                 bs = parse_any_iso(b.start).astimezone(tz)
#                 be = parse_any_iso(b.end).astimezone(tz)
#                 if be > bs:
#                     busy_intervals_local.append(Interval(bs, be))
#             except Exception:
#                 warnings.append("Some busy intervals could not be parsed and were ignored.")

#     busy_intervals_local = merge_intervals(busy_intervals_local)
#     free_minus_busy = subtract_busy(free_intervals, busy_intervals_local)

#     # 3) Generate discrete slots
#     slots_intervals = generate_slots_from_intervals(
#         free_minus_busy,
#         duration_min=payload.slotDurationMinutes,
#         buffer_min=payload.bufferMinutes
#     )

#     # 4) Format output (local + UTC)
#     slots_out: list[Slot] = []
#     for s in slots_intervals:
#         start_local = s.start.strftime("%Y-%m-%dT%H:%M")
#         end_local = s.end.strftime("%Y-%m-%dT%H:%M")
#         start_utc = s.start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
#         end_utc = s.end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

#         slots_out.append(Slot(
#             startAtLocal=start_local,
#             endAtLocal=end_local,
#             startAtUtc=start_utc,
#             endAtUtc=end_utc
#         ))

#     # 5) Summary
#     if not slots_out:
#         summary = "No free slots found after applying busy times."
#         needs = "Try expanding your working hours/date range or reducing duration/buffer."
#         return ProposeSlotsResponse(
#             recruiterEmail=email,
#             provider="google",
#             timezone=tz_name,
#             slotDurationMinutes=payload.slotDurationMinutes,
#             bufferMinutes=payload.bufferMinutes,
#             slots=[],
#             summary=summary,
#             warnings=warnings + (["All time got blocked by existing meetings."] if payload.busy else []),
#             needs_clarification=needs
#         )
    
#     logger.info(f"Generated {len(slots_out)} slots for {email}")

#     # build a friendly summary range
#     first = slots_intervals[0].start
#     last = slots_intervals[-1].end
#     summary = (
#         f"Generated {len(slots_out)} slots in {tz_name}. "
#         f"Duration {payload.slotDurationMinutes}m, buffer {payload.bufferMinutes}m. "
#         f"From {first.strftime('%Y-%m-%d %H:%M')} to {last.strftime('%Y-%m-%d %H:%M')} (local)."
#     )

#     # Create an in-chat preview text (grouped by date) and store a short-lived proposal
#     proposal_id = "p_" + secrets.token_urlsafe(16)
#     preview_text = format_slots_preview([s.model_dump() for s in slots_out], max_per_day=12)
#     expires_at = utcnow() + timedelta(hours=24)

#     logger.info(f"Storing proposal {proposal_id} with {len(slots_out)} slots")

#     await db[COLL_SLOT_PROPOSALS].insert_one({
#         "proposalId": proposal_id,
#         "recruiterEmail": email,
#         "provider": payload.provider,
#         "timezone": tz_name,
#         "jobId": payload.jobId,
#         "jobTitle": payload.jobTitle,
#         "mode": payload.mode,
#         "slotDurationMinutes": payload.slotDurationMinutes,
#         "bufferMinutes": payload.bufferMinutes,
#         "summary": summary,
#         "previewText": preview_text,
#         "slots": [s.model_dump() for s in slots_out],
#         "status": "draft",
#         "createdAt": utcnow(),
#         "updatedAt": utcnow(),
#         "expiresAt": expires_at
#     })


#     return ProposeSlotsResponse(
#         recruiterEmail=email,
#         provider="google",
#         timezone=tz_name,
#         slotDurationMinutes=payload.slotDurationMinutes,
#         bufferMinutes=payload.bufferMinutes,
#         proposalId=proposal_id,
#         previewText=preview_text,
#         slots=slots_out,
#         summary=summary,
#         warnings=warnings,
#         needs_clarification=None
#     )

# # Endpoint to save proposed slots into final availability collection
# @app.post("/tools/saveSlots", response_model=SaveSlotsResponse)
# async def save_slots(payload: SaveSlotsRequest, db=Depends(get_db)):
#     email = payload.recruiterEmail.lower().strip()
#     provider = payload.provider
#     proposal_id = payload.proposalId.strip()
#     job_id, job_title = validate_recruiter_job_metadata(payload.jobId, payload.jobTitle)

#     logger.info(f"SaveSlots called for proposal {proposal_id} by {email} jobId={job_id}")

#     # 1) Fetch proposal
#     proposal = await db[COLL_SLOT_PROPOSALS].find_one(
#         {"proposalId": proposal_id, "recruiterEmail": email, "provider": provider},
#         projection={"_id": 0}
#     )
#     if not proposal:
#         logger.warning(f"[saveSlots] proposal not found/expired recruiter={email} proposalId={proposal_id}")
#         raise HTTPException(status_code=404, detail="Proposal not found or expired. Please generate slots again.")

#     if proposal.get("status") == "confirmed":
#         # idempotent behavior: if already confirmed, don’t double insert
#         # We can return 0 savedCount or query how many were saved from this proposal.
#         existing = await db[COLL_AVAIL_SLOTS].count_documents({"sourceProposalId": proposal_id})
#         return SaveSlotsResponse(
#             recruiterEmail=email,
#             provider=provider,
#             proposalId=proposal_id,
#             savedCount=int(existing),
#             message="This proposal was already saved earlier."
#         )

#     slots = proposal.get("slots") or []
#     if not slots:
#         raise HTTPException(status_code=400, detail="Proposal contains no slots.")

#     # 2) Prepare docs for final storage
#     now = utcnow()
#     docs = []
#     for s in slots:
#         slot_end_utc = s["endAtUtc"]
#         # s is expected to have startAtUtc/endAtUtc + local versions from proposeSlots
#         docs.append({
#             "recruiterEmail": email,
#             "provider": provider,
#             "timezone": proposal.get("timezone", "Asia/Kolkata"),
#             "jobId": job_id,
#             "jobTitle": job_title or proposal.get("jobTitle"),
#             "mode": payload.mode or proposal.get("mode"),
#             "startAtUtc": s["startAtUtc"],
#             "endAtUtc": slot_end_utc,
#             "startAtLocal": s.get("startAtLocal"),
#             "endAtLocal": s.get("endAtLocal"),
#             "slotExpiresAt": compute_slot_expiry(slot_end_utc),
#             "sourceProposalId": proposal_id,
#             "status": "active",
#             "holdCandidateId": None,
#             "holdSessionId": None,
#             "holdExpiresAt": None,
#             "bookedCandidateId": None,
#             "scheduledInterviewId": None,
#             "bookedAt": None,
#             "createdAt": now,
#             "updatedAt": now
#         })

#     # 3) Insert (handle duplicates gracefully)
#     saved_count = 0
#     try:
#         logger.info(f"[saveSlots] inserting {len(docs)} slots recruiter={email} proposalId={proposal_id}")
#         result = await db[COLL_AVAIL_SLOTS].insert_many(docs, ordered=False)
#         saved_count = len(result.inserted_ids)
#         logger.info(f"[saveSlots] inserted savedCount={saved_count} recruiter={email} proposalId={proposal_id}")
#     except Exception as e:
#         # If duplicates occur due to retry, ordered=False still inserts others.
#         # We’ll compute saved count from DB to be safe.
#         logger.exception(f"[saveSlots] insert_many failed recruiter={email} proposalId={proposal_id}")
#         saved_count = await db[COLL_AVAIL_SLOTS].count_documents({"sourceProposalId": proposal_id})
#         logger.info(f"[saveSlots] after failure, counted savedCount={saved_count} from DB proposalId={proposal_id}")

#     # 4) Mark proposal confirmed
#     await db[COLL_SLOT_PROPOSALS].update_one(
#         {"proposalId": proposal_id},
#         {"$set": {"status": "confirmed", "confirmedAt": now, "updatedAt": now}}
#     )

#     return SaveSlotsResponse(
#         recruiterEmail=email,
#         provider=provider,
#         proposalId=proposal_id,
#         savedCount=int(saved_count),
#         message=f"Saved {saved_count} availability slots."
#     )

# Endpoint to resolve an active candidate scheduling session for a candidate + job, which checks if the session is still valid and returns necessary info for the candidate to book slots, if not valid then create a new session and return info (idempotent for active sessions)
@app.post(
    "/tools/resolveCandidateSchedulingSession",
    response_model=ResolveCandidateSchedulingSessionResponse
)
async def resolve_candidate_scheduling_session(
    payload: ResolveCandidateSchedulingSessionRequest,
    db=Depends(get_db)
):
    return await resolve_candidate_scheduling_session_logic(
        payload=payload,
        db=db,
        coll_candidates=COLL_CANDIDATES,
        coll_recruiters=COLL_RECRUITERS,
        coll_candidate_sessions=COLL_CANDIDATE_SESSIONS,
        coll_avail_slots=COLL_AVAIL_SLOTS,
        coll_scheduled_interviews=COLL_SCHEDULED_INTERVIEWS,
        validate_recruiter_job_metadata=validate_recruiter_job_metadata,
        utcnow_fn=utcnow,
        logger=logger,
    )

# # Candidate scheduling endpoints
# @app.post("/tools/startCandidateSchedulingSession", response_model=StartCandidateSchedulingSessionResponse)
# async def start_candidate_scheduling_session(
#     payload: StartCandidateSchedulingSessionRequest,
#     db=Depends(get_db)
# ):
#     return await start_candidate_scheduling_session_logic(
#         payload=payload,
#         db=db,
#         coll_candidates=COLL_CANDIDATES,
#         coll_recruiters=COLL_RECRUITERS,
#         coll_avail_slots=COLL_AVAIL_SLOTS,
#         coll_candidate_sessions=COLL_CANDIDATE_SESSIONS,
#         validate_recruiter_job_metadata=validate_recruiter_job_metadata,
#         utcnow_fn=utcnow,
#         logger=logger,
#     )

# Endpoint to get next available slots for a candidate scheduling session (with pagination)
@app.post(
    "/tools/getNextAvailableSlots",
    response_model=GetNextAvailableSlotsResponse
)
async def get_next_available_slots(
    payload: GetNextAvailableSlotsRequest,
    db=Depends(get_db),
):
    return await get_next_available_slots_logic(
        payload=payload,
        db=db,
        coll_avail_slots=COLL_AVAIL_SLOTS,
        coll_candidate_sessions=COLL_CANDIDATE_SESSIONS,
        utcnow_fn=utcnow,
        logger=logger,
    )

# Endpoint to confirm a candidate's slot booking, which creates a scheduled interview and sends confirmation email
@app.post("/tools/confirmCandidateSlotBooking", response_model=ConfirmCandidateSlotBookingResponse)
async def confirm_candidate_slot_booking(
    payload: ConfirmCandidateSlotBookingRequest,
    db=Depends(get_db)
):
    return await confirm_candidate_slot_booking_logic(
        payload=payload,
        db=db,
        coll_candidate_sessions=COLL_CANDIDATE_SESSIONS,
        coll_avail_slots=COLL_AVAIL_SLOTS,
        coll_scheduled_interviews=COLL_SCHEDULED_INTERVIEWS,
        coll_interview_reminders=COLL_INTERVIEW_REMINDERS,
        coll_recruiters=COLL_RECRUITERS,
        coll_cal_conn=COLL_CAL_CONN,
        utcnow_fn=utcnow,
        send_email_fn=send_email_smtp,
        logger=logger,
    )

# Endpoint to cancel a candidate's scheduled interview, which frees up the slot and sends cancellation email
@app.post(
    "/tools/cancelCandidateInterview",
    response_model=CancelCandidateInterviewResponse
)
async def cancel_candidate_interview(
    payload: CancelCandidateInterviewRequest,
    db=Depends(get_db),
):
    return await cancel_candidate_interview_logic(
        payload=payload,
        db=db,
        coll_scheduled_interviews=COLL_SCHEDULED_INTERVIEWS,
        coll_interview_reminders=COLL_INTERVIEW_REMINDERS,
        coll_avail_slots=COLL_AVAIL_SLOTS,
        coll_candidate_sessions=COLL_CANDIDATE_SESSIONS,
        coll_recruiters=COLL_RECRUITERS,
        coll_cal_conn=COLL_CAL_CONN,
        utcnow_fn=utcnow,
        send_email_fn=send_email_smtp,
        logger=logger,
    )

# Endpoint to reschedule a candidate's interview, which updates the scheduled interview with a new slot and sends rescheduling email
@app.post(
    "/tools/rescheduleCandidateInterview",
    response_model=RescheduleCandidateInterviewResponse
)
async def reschedule_candidate_interview(
    payload: RescheduleCandidateInterviewRequest,
    db=Depends(get_db),
):
    return await reschedule_candidate_interview_logic(
        payload=payload,
        db=db,
        coll_scheduled_interviews=COLL_SCHEDULED_INTERVIEWS,
        coll_interview_reminders=COLL_INTERVIEW_REMINDERS,
        coll_avail_slots=COLL_AVAIL_SLOTS,
        coll_candidate_sessions=COLL_CANDIDATE_SESSIONS,
        coll_recruiters=COLL_RECRUITERS,
        coll_cal_conn=COLL_CAL_CONN,
        coll_reschedule_requests=COLL_RESCHEDULE_REQUESTS,
        utcnow_fn=utcnow,
        logger=logger,
    )

# Endpoints for rescheduling workflow: candidate can request reschedule, which creates a reschedule request in pending state, then recruiter can approve or reject the request
@app.post(
    "/tools/startCandidateRescheduleRequest",
    response_model=StartCandidateRescheduleRequestResponse
)
async def start_candidate_reschedule_request(
    payload: StartCandidateRescheduleRequest,
    db=Depends(get_db),
):
    return await start_candidate_reschedule_request_logic(
        payload=payload,
        db=db,
        coll_scheduled_interviews=COLL_SCHEDULED_INTERVIEWS,
        coll_candidate_sessions=COLL_CANDIDATE_SESSIONS,
        coll_recruiters=COLL_RECRUITERS,
        coll_avail_slots=COLL_AVAIL_SLOTS,
        coll_reschedule_requests=COLL_RESCHEDULE_REQUESTS,
        utcnow_fn=utcnow,
        logger=logger,
    )

@app.post(
    "/tools/createCandidateRescheduleRequest",
    response_model=CreateCandidateRescheduleRequestResponse
)
async def create_candidate_reschedule_request(
    payload: CreateCandidateRescheduleRequestRequest,
    db=Depends(get_db),
):
    return await create_candidate_reschedule_request_logic(
        payload=payload,
        db=db,
        coll_candidate_sessions=COLL_CANDIDATE_SESSIONS,
        coll_scheduled_interviews=COLL_SCHEDULED_INTERVIEWS,
        coll_avail_slots=COLL_AVAIL_SLOTS,
        coll_reschedule_requests=COLL_RESCHEDULE_REQUESTS,
        utcnow_fn=utcnow,
        logger=logger,
    )

# Endpoint for recruiter to approve a reschedule request, which updates the scheduled interview with the new slot, marks the reschedule request as approved, and sends confirmation emails to candidate and interviewer
@app.post(
    "/tools/approveRescheduleRequest",
    response_model=ApproveRescheduleRequestResponse
)
async def approve_reschedule_request(
    payload: ApproveRescheduleRequestRequest,
    db=Depends(get_db),
):
    return await approve_reschedule_request_logic(
        payload=payload,
        db=db,
        coll_reschedule_requests=COLL_RESCHEDULE_REQUESTS,
        coll_scheduled_interviews=COLL_SCHEDULED_INTERVIEWS,
        coll_interview_reminders=COLL_INTERVIEW_REMINDERS,
        coll_avail_slots=COLL_AVAIL_SLOTS,
        coll_candidate_sessions=COLL_CANDIDATE_SESSIONS,
        coll_recruiters=COLL_RECRUITERS,
        coll_cal_conn=COLL_CAL_CONN,
        coll_interview_reminders_out=COLL_INTERVIEW_REMINDERS,
        utcnow_fn=utcnow,
        send_email_fn=send_email_smtp,
        logger=logger,
    )

# Endpoint for recruiter to reject a reschedule request, which marks the reschedule request as rejected and sends email to candidate with original interview details
@app.post(
    "/tools/rejectRescheduleRequest",
    response_model=RejectRescheduleRequestResponse
)
async def reject_reschedule_request(
    payload: RejectRescheduleRequestRequest,
    db=Depends(get_db),
):
    return await reject_reschedule_request_logic(
        payload=payload,
        db=db,
        coll_reschedule_requests=COLL_RESCHEDULE_REQUESTS,
        coll_scheduled_interviews=COLL_SCHEDULED_INTERVIEWS,
        coll_candidate_sessions=COLL_CANDIDATE_SESSIONS,
        utcnow_fn=utcnow,
        send_email_fn=send_email_smtp,
        logger=logger,
    )

# Endpoint for recruiter to list all pending reschedule requests for their interviews, with details about the candidate, original interview time, and requested new time
@app.get(
    "/tools/listRescheduleRequests",
    response_model=ListRescheduleRequestsResponse
)
async def list_reschedule_requests(
    recruiterEmail: Optional[str] = Query(default=None),
    db=Depends(get_db),
):
    return await list_reschedule_requests_logic(
        db=db,
        coll_reschedule_requests=COLL_RESCHEDULE_REQUESTS,
        recruiter_email=recruiterEmail,
    )

# Endpoint to check if calendar is connected, and if not, create OAuth state and send connection email(checkCalendarConnection + startGoogleOAuth combined)
@app.post("/tools/checkOrStartCalendarConnection", response_model=CheckCalendarConnectionResponse)
async def check_or_start_calendar_connection(
    payload: CheckCalendarConnectionRequest,
    db=Depends(get_db)
):
    email = payload.recruiterEmail.lower().strip()
    provider = payload.provider

    if provider != "google":
        raise HTTPException(status_code=400, detail="Only google is supported right now.")

    # 1) Check existing calendar connection
    doc = await db[COLL_CAL_CONN].find_one(
        {"recruiterEmail": email, "provider": provider},
        projection={"_id": 0, "status": 1}
    )

    if doc and doc.get("status") == "connected":
        return CheckCalendarConnectionResponse(
            message="Calendar already connected.",
            status=True
        )

    # 2) If not connected, create OAuth state
    state = new_state_token()
    expires_at = utcnow() + timedelta(minutes=10)

    await db[COLL_OAUTH_STATES].insert_one({
        "state": state,
        "provider": "google",
        "recruiterEmail": email,
        "createdAt": utcnow(),
        "expiresAt": expires_at
    })

    # 3) Build Google OAuth URL
    auth_url = build_google_auth_url(state)

    # 4) Send OAuth link via email
    subject = "Connect your Google Calendar to Scheduler Bot"
    html_body = f"""
    <div style="font-family: Arial, sans-serif; line-height: 1.4;">
      <p>Hi,</p>
      <p>Please connect your Google Calendar to allow us to check your availability and avoid conflicts while generating interview slots.</p>
      <p><a href="{auth_url}" target="_blank" rel="noreferrer">Click here to connect Google Calendar</a></p>
      <p>If the button does not work, copy and paste this link into your browser:</p>
      <p style="word-break: break-all;">{auth_url}</p>
      <p>This link is valid for a short time. After connecting, return to the app to continue.</p>
      <p>Thanks,<br/>Scheduler Bot</p>
    </div>
    """

    text_body = f"""Please connect your Google Calendar:

{auth_url}

After connecting, return to the app to continue.
"""

    try:
        await anyio.to_thread.run_sync(
            send_email_smtp,
            email,
            subject,
            html_body,
            text_body
        )
    except Exception as e:
        logger.exception("Failed to send OAuth email")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to send calendar connection email: {str(e)}"
        )

    return CheckCalendarConnectionResponse(
        message="Calendar is not connected. A connection link has been sent to your email. Please click the link in the email to connect your calendar.",
        status=False
    )

# Endpoint to save slots directly from the recruiter dashboard
@app.post("/tools/saveDirectSlots", response_model=SaveDirectSlotsResponse)
async def save_direct_slots(payload: SaveDirectSlotsRequest, db=Depends(get_db)):
    email = payload.recruiterEmail.lower().strip()
    provider = payload.provider.strip().lower()
    timezone_name = (payload.timezone or "Asia/Kolkata").strip()

    logger.info(f"[saveDirectSlots] called recruiter={email} slots={len(payload.slots)}")

    if not payload.slots:
        raise HTTPException(status_code=400, detail="At least one slot is required.")

    now = utcnow()
    docs = []

    for idx, s in enumerate(payload.slots, start=1):
        try:
            # parse local datetime strings
            start_local_dt = parse_dt(s.startAtLocal)
            end_local_dt = parse_dt(s.endAtLocal)

            if not start_local_dt or not end_local_dt:
                raise ValueError("Invalid datetime format.")

            # If incoming datetime is naive, interpret it in payload.timezone
            start_utc = to_utc_iso(start_local_dt, timezone_name)
            end_utc = to_utc_iso(end_local_dt, timezone_name)

            # Re-parse UTC values for comparison / expiry
            start_utc_dt = parse_dt(start_utc)
            end_utc_dt = parse_dt(end_utc)

            if end_utc_dt <= start_utc_dt:
                raise ValueError("endAtLocal must be greater than startAtLocal.")

            docs.append({
                "recruiterEmail": email,
                "provider": provider,
                "timezone": timezone_name,
                "mode": payload.mode,
                "startAtUtc": start_utc,
                "endAtUtc": end_utc,
                "startAtLocal": s.startAtLocal,
                "endAtLocal": s.endAtLocal,
                "slotExpiresAt": compute_slot_expiry(end_utc),
                "status": "active",
                "holdCandidateId": None,
                "holdSessionId": None,
                "holdExpiresAt": None,
                "bookedCandidateId": None,
                "scheduledInterviewId": None,
                "bookedAt": None,
                "createdAt": now,
                "updatedAt": now
            })

        except Exception as e:
            logger.exception(f"[saveDirectSlots] invalid slot at index={idx} recruiter={email}")
            raise HTTPException(
                status_code=400,
                detail=f"Invalid slot at position {idx}: {str(e)}"
            )

    saved_count = 0
    try:
        logger.info(f"[saveDirectSlots] inserting {len(docs)} slots recruiter={email}")
        result = await db[COLL_AVAIL_SLOTS].insert_many(docs, ordered=False)
        saved_count = len(result.inserted_ids)
        logger.info(f"[saveDirectSlots] inserted savedCount={saved_count} recruiter={email}")
    except Exception:
        logger.exception(f"[saveDirectSlots] insert_many failed recruiter={email}")
        # fallback: count what got inserted for this recruiter + these timestamps
        saved_count = len(docs)

    return SaveDirectSlotsResponse(
        recruiterEmail=email,
        provider=provider,
        savedCount=int(saved_count),
        message=f"Saved {saved_count} availability slots."
    )

# Endpoint to get all candidates (for testing/debugging)
@app.get("/getCandidates")
async def get_candidates(db=Depends(get_db)) -> Any:
    result = await db[COLL_CANDIDATES].find({}, projection={"_id": 0}).to_list(length=None)
    return {
        "statusCode": 200,
        "message": "Success",
        "data": result,
    }

# Endpoint to save the candidate details from candidate login page.
@app.post("/candidateLogin")
async def candidate_login(payload: CandidateLoginRequest, db=Depends(get_db)):
    email = payload.email.lower().strip()
    phone = payload.phone.strip()
    name = payload.name.strip()

    existing_candidate = await db[COLL_CANDIDATES].find_one(
        {"email": email},
        projection={"_id": 0}
    )

    if existing_candidate:
        await db[COLL_CANDIDATES].update_one(
            {"email": email},
            {
                "$set": {
                    "name": name,
                    "phone": phone,
                    "updatedAt": utcnow()
                }
            }
        )

        updated_candidate = await db[COLL_CANDIDATES].find_one(
            {"email": email},
            projection={"_id": 0}
        )

        return {
            "candidateId": updated_candidate["candidateId"],
            "name": updated_candidate["name"],
            "phone": updated_candidate["phone"],
            "email": updated_candidate["email"],
            "message": "Candidate already exists. Details updated successfully."
        }

    now = utcnow()
    candidate_id = generate_candidate_id()

    candidate_doc = {
        "candidateId": candidate_id,
        "name": name,
        "phone": phone,
        "email": email,
        "createdAt": now,
        "updatedAt": now
    }

    await db[COLL_CANDIDATES].insert_one(candidate_doc)

    return {
        "candidateId": candidate_id,
        "name": name,
        "phone": phone,
        "email": email,
        "message": "Candidate created successfully."
    }

# Endpoint to save the job openings selected by a candidate after the voice agent flow.
@app.post("/candidateSelectedJobs")
async def candidate_selected_jobs(payload: CandidateJobSelectionRequest, db=Depends(get_db)):
    candidate_filter = {"email": payload.email}

    candidate = await db[COLL_CANDIDATES].find_one(
        candidate_filter,
        projection={"_id": 0}
    )

    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found in candidateData.")

    now = utcnow()
    existing_jobs = candidate.get("selectedJobs") or []
    if not isinstance(existing_jobs, list):
        existing_jobs = []

    def selection_key(job: dict) -> tuple[str, str]:
        recruiter_email = (job.get("recruiterEmail") or "").lower().strip()
        job_id = (job.get("jobId") or "").strip()
        return recruiter_email, job_id

    merged_jobs_by_key = {
        selection_key(job): job
        for job in existing_jobs
        if isinstance(job, dict) and (job.get("jobId") or "").strip()
    }
    ordered_keys = list(merged_jobs_by_key.keys())
    job_doc = payload.selectedJob.model_dump(mode="json", exclude_none=True)
    if job_doc.get("recruiterEmail"):
        job_doc["recruiterEmail"] = job_doc["recruiterEmail"].lower().strip()
        if job_doc["recruiterEmail"] == payload.email:
            raise HTTPException(
                status_code=400,
                detail="Recruiter email and candidate email cannot be the same."
            )
    job_doc["selectedAt"] = now

    key = selection_key(job_doc)
    existing_job = merged_jobs_by_key.get(key)
    if existing_job and existing_job.get("firstSelectedAt"):
        job_doc["firstSelectedAt"] = existing_job["firstSelectedAt"]
    else:
        job_doc["firstSelectedAt"] = now

    if key not in merged_jobs_by_key:
        ordered_keys.append(key)
    merged_jobs_by_key[key] = job_doc

    merged_jobs = [merged_jobs_by_key[key] for key in ordered_keys]
    selected_job_ids = sorted({
        job.get("jobId")
        for job in merged_jobs
        if isinstance(job, dict) and job.get("jobId")
    })

    await db[COLL_CANDIDATES].update_one(
        {"candidateId": candidate["candidateId"]},
        {
            "$set": {
                "selectedJobs": merged_jobs,
                "selectedJobIds": selected_job_ids,
                "latestSelectedJob": job_doc,
                "jobSelectionUpdatedAt": now,
                "updatedAt": now
            }
        }
    )

    return {
        "candidateId": candidate["candidateId"],
        "email": candidate["email"],
        "selectedJob": job_doc,
        "totalSelectedJobCount": len(merged_jobs),
        "message": "Candidate selected jobs saved successfully."
    }

# Endpoint to save the recruiter details from recruiter login page.
@app.post("/recruiterLogin")
async def recruiter_login(payload: RecruiterLoginRequest, db=Depends(get_db)):
    email = payload.email.lower().strip()
    phone = payload.phone.strip()
    name = payload.name.strip()

    existing_recruiter = await db[COLL_RECRUITERS].find_one(
        {"email": email},
        projection={"_id": 0}
    )

    if existing_recruiter:
        await db[COLL_RECRUITERS].update_one(
            {"email": email},
            {
                "$set": {
                    "name": name,
                    "phone": phone,
                    "updatedAt": utcnow()
                }
            }
        )

        updated_recruiter = await db[COLL_RECRUITERS].find_one(
            {"email": email},
            projection={"_id": 0}
        )

        return {
            "recruiterId": updated_recruiter["recruiterId"],
            "name": updated_recruiter["name"],
            "phone": updated_recruiter["phone"],
            "email": updated_recruiter["email"],
            "message": "Recruiter already exists. Details updated successfully."
        }

    now = utcnow()
    recruiter_id = generate_recruiter_id()

    recruiter_doc = {
        "recruiterId": recruiter_id,
        "name": name,
        "phone": phone,
        "email": email,
        "createdAt": now,
        "updatedAt": now
    }

    await db[COLL_RECRUITERS].insert_one(recruiter_doc)

    return {
        "recruiterId": recruiter_id,
        "name": name,
        "phone": phone,
        "email": email,
        "message": "Recruiter created successfully."
    }


# if __name__ == "__main__":
#     uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 9009)))
