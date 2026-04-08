from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx
from bson import ObjectId
from fastapi import HTTPException

from oauth_google_refresh import parse_dt, is_expired, refresh_access_token, compute_expires_at
from schemas import CancelCandidateInterviewRequest, CancelCandidateInterviewResponse

GOOGLE_DELETE_EVENT_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}"

def _parse_mongo_id(raw_id: str) -> ObjectId:
    try:
        return ObjectId(raw_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid slotId.")
    
def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

async def _get_valid_google_access_token(db, coll_cal_conn: str, recruiter_email: str) -> tuple[str, str]:
    conn = await db[coll_cal_conn].find_one({
        "recruiterEmail": recruiter_email,
        "provider": "google",
        "status": "connected",
    })
    if not conn:
        raise HTTPException(status_code=404, detail="Recruiter Google calendar connection not found.")

    token = conn.get("token") or {}
    access_token = token.get("accessToken")
    refresh_token = token.get("refreshToken")
    expires_at = token.get("expiresAt")
    calendar_id = conn.get("calendarId") or recruiter_email

    if not access_token:
        raise HTTPException(status_code=400, detail="Recruiter access token missing.")

    if is_expired(expires_at):
        if not refresh_token:
            raise HTTPException(status_code=401, detail="Refresh token missing for recruiter calendar.")
        
        refreshed = await refresh_access_token(refresh_token)
        access_token = refreshed["access_token"]
        new_refresh_token = refreshed.get("refresh_token") or refresh_token
        new_expires_at = compute_expires_at(refreshed.get("expires_in", 3600))

        await db[coll_cal_conn].update_one(
            {"_id": conn["_id"]},
            {"$set": {
                "token.accessToken": access_token,
                "token.refreshToken": new_refresh_token,
                "token.expiresAt": new_expires_at,
                "updatedAt": datetime.now(timezone.utc),
            }}
        )

    return access_token, calendar_id

async def _delete_google_calendar_event(
    *,
    access_token: str,
    calendar_id: str,
    event_id: str,
) -> None:
    url = GOOGLE_DELETE_EVENT_URL.format(calendar_id=calendar_id, event_id=event_id)
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.delete(url, headers=headers, params={"sendUpdates": "all"})

        if resp.status_code == 404:
            return

        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="Google authorization invalid while cancelling event.")

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"Google Calendar event cancellation failed: {str(exc)}")


def _build_cancellation_messages(
    *,
    candidate_name: str,
    candidate_email: str,
    recruiter_email: str,
    job_title: Optional[str],
    slot_display_text: str,
) -> tuple[tuple[str, str, str], tuple[str, str, str], str]:
    role_text = f" for the {job_title} role" if job_title else ""
    subject = f"Interview cancelled{role_text}"

    candidate_html = f"""
    <p>Hi {candidate_name},</p>
    <p>Your interview{role_text} scheduled for <strong>{slot_display_text}</strong> has been cancelled.</p>
    <p>If you’d like to reschedule, just reply on WhatsApp.</p>
    """.strip()

    candidate_text = (
        f"Hi {candidate_name},\n\n"
        f"Your interview{role_text} scheduled for {slot_display_text} has been cancelled.\n"
        f"If you'd like to reschedule, just reply on WhatsApp."
    )

    recruiter_html = f"""
    <p>Hi,</p>
    <p>An interview{role_text} has been cancelled.</p>
    <p><strong>Candidate:</strong> {candidate_name}</p>
    <p><strong>Candidate email:</strong> {candidate_email}</p>
    <p><strong>Scheduled time:</strong> {slot_display_text}</p>
    <p>Recruiter email: {recruiter_email}</p>
    """.strip()

    recruiter_text = (
        f"An interview{role_text} has been cancelled.\n"
        f"Candidate: {candidate_name}\n"
        f"Candidate email: {candidate_email}\n"
        f"Scheduled time: {slot_display_text}\n"
        f"Recruiter email: {recruiter_email}"
    )

    whatsapp_text = (
        f"Your interview has been cancelled successfully."
        f"{' If you’d like, I can help you reschedule.'}"
    )

    return (
        (subject, candidate_html, candidate_text),
        (subject, recruiter_html, recruiter_text),
        whatsapp_text,
    )



async def _get_recruiter_doc(db, coll_recruiters: str, recruiter_email: str) -> dict[str, Any]:
    recruiter = await db[coll_recruiters].find_one({"email": recruiter_email})
    if not recruiter:
        return {
            "recruiterId": None,
            "recruiterName": None,
            "recruiterPhone": None,
            "recruiterEmail": recruiter_email,
        }
    return {
        "recruiterId": (recruiter.get("recruiterId") or "").strip() or None,
        "recruiterName": (recruiter.get("name") or "").strip() or None,
        "recruiterPhone": (recruiter.get("phone") or "").strip() or None,
        "recruiterEmail": (recruiter.get("email") or recruiter_email).strip().lower(),
    }

def _build_slot_display(interview_doc: dict[str, Any]) -> str:
    start_local = interview_doc.get("startAtLocal") or interview_doc.get("startAtUtc")
    end_local = interview_doc.get("endAtLocal") or interview_doc.get("endAtUtc")
    timezone_name = interview_doc.get("timezone") or "Asia/Kolkata"
    return f"{start_local} to {end_local} ({timezone_name})"


async def cancel_candidate_interview_logic(
    *,
    payload: CancelCandidateInterviewRequest,
    db,
    coll_scheduled_interviews: str,
    coll_interview_reminders: str,
    coll_avail_slots: str,
    coll_candidate_sessions: str,
    coll_recruiters: str,
    coll_cal_conn: str,
    utcnow_fn: Callable[[], Any],
    send_email_fn: Callable[[str, str, str, Optional[str]], None],
    logger,
) -> CancelCandidateInterviewResponse:
    scheduled_interview_id = payload.scheduledInterviewId.strip()
    cancelled_by = payload.cancelledBy.strip() or "candidate"
    now = utcnow_fn()

    logger.info("[cancelCandidateInterview] scheduledInterviewId=%s", scheduled_interview_id)

    interview = await db[coll_scheduled_interviews].find_one({
        "scheduledInterviewId": scheduled_interview_id
    })
    if not interview:
        raise HTTPException(status_code=404, detail="Scheduled interview not found.")

    interview_status = (interview.get("status") or "").strip().lower()
    if interview_status == "cancelled":
        raise HTTPException(status_code=400, detail="Interview is already cancelled.")
    if interview_status == "rescheduled":
        raise HTTPException(
            status_code=400,
            detail="This interview has already been replaced by a newer scheduled interview and cannot be cancelled."
        )

    candidate_id = (interview.get("candidateId") or "").strip()
    candidate_name = (interview.get("candidateName") or "").strip()
    candidate_email = (interview.get("candidateEmail") or "").strip()
    recruiter_email = (interview.get("recruiterEmail") or "").strip().lower()
    job_id = (interview.get("jobId") or "").strip()
    job_title = interview.get("jobTitle")
    slot_id = (interview.get("slotId") or "").strip()
    session_id = interview.get("sessionId")
    calendar_event_id = interview.get("calendarEventId")
    start_at_utc = interview.get("startAtUtc")

    recruiter = await _get_recruiter_doc(db, coll_recruiters, recruiter_email)

    if calendar_event_id:
        access_token, calendar_id = await _get_valid_google_access_token(db, coll_cal_conn, recruiter_email)
        await _delete_google_calendar_event(
            access_token=access_token,
            calendar_id=calendar_id,
            event_id=calendar_event_id,
        )

    await db[coll_scheduled_interviews].update_one(
        {"scheduledInterviewId": scheduled_interview_id},
        {"$set": {
            "status": "cancelled",
            "cancelledBy": cancelled_by,
            "cancelledAt": now,
            "updatedAt": now,
        }}
    )

    if session_id:
        await db[coll_candidate_sessions].update_one(
            {"sessionId": session_id},
            {"$set": {
                "status": "cancelled",
                "updatedAt": now,
            }}
        )

    slot_reopened = False
    if slot_id and start_at_utc:
        try:
            slot_object_id = _parse_mongo_id(slot_id)
            if _parse_utc(start_at_utc) > now.astimezone(timezone.utc):
                reopen_result = await db[coll_avail_slots].update_one(
                    {
                        "_id": slot_object_id,
                        "scheduledInterviewId": scheduled_interview_id,
                        "status": "booked",
                    },
                    {"$set": {
                        "status": "active",
                        "bookedCandidateId": None,
                        "scheduledInterviewId": None,
                        "bookedAt": None,
                        "updatedAt": now,
                    }}
                )
                slot_reopened = reopen_result.modified_count == 1
        except Exception:
            logger.exception("[cancelCandidateInterview] failed to reopen slot for scheduledInterviewId=%s", scheduled_interview_id)

    cancelled_reminders_result = await db[coll_interview_reminders].update_many(
        {
            "scheduledInterviewId": scheduled_interview_id,
            "status": "pending",
        },
        {"$set": {
            "status": "cancelled",
            "updatedAt": now,
        }}
    )

    slot_display_text = _build_slot_display(interview)

    (candidate_subject, candidate_html, candidate_text), (recruiter_subject, recruiter_html, recruiter_text), whatsapp_text = _build_cancellation_messages(
        candidate_name=candidate_name,
        candidate_email=candidate_email,
        recruiter_email=recruiter.get("recruiterEmail") or recruiter_email,
        job_title=job_title,
        slot_display_text=slot_display_text,
    )

    if candidate_email:
        send_email_fn(candidate_email, candidate_subject, candidate_html, candidate_text)
    if recruiter.get("recruiterEmail"):
        send_email_fn(recruiter["recruiterEmail"], recruiter_subject, recruiter_html, recruiter_text)

    return CancelCandidateInterviewResponse(
        scheduledInterviewId=scheduled_interview_id,
        sessionId=session_id,
        candidateId=candidate_id,
        recruiterEmail=recruiter_email,
        jobId=job_id,
        jobTitle=job_title,
        slotId=slot_id,
        slotReopened=slot_reopened,
        cancelledReminderCount=cancelled_reminders_result.modified_count,
        messageText=whatsapp_text,
        message="Interview cancelled successfully.",
    )