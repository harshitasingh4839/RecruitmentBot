from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import httpx
from fastapi import HTTPException

try:
    from bson import ObjectId
except Exception:  # pragma: no cover
    ObjectId = None

from oauth_google_refresh import parse_dt, is_expired, refresh_access_token, compute_expires_at
from schemas import (
    ConfirmCandidateSlotBookingRequest,
    ConfirmCandidateSlotBookingResponse,
)

GOOGLE_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"

def build_slot_display(slot: dict[str, Any], tz_name: str) -> str:
    start_local = slot.get("startAtLocal") or slot.get("startAtUtc")
    end_local = slot.get("endAtLocal") or slot.get("endAtUtc")
    return f"{start_local} to {end_local} ({tz_name})"


async def _get_recruiter_doc(db, coll_recruiters: str, recruiter_email: str) -> dict[str, Any]:
    recruiter = await db[coll_recruiters].find_one({"email": recruiter_email})
    if not recruiter:
        raise HTTPException(status_code=404, detail="Recruiter not found in recruiterData.")
    recruiter_id = (recruiter.get("recruiterId") or "").strip()
    recruiter_name = (recruiter.get("name") or "").strip()
    recruiter_phone = (recruiter.get("phone") or "").strip()
    recruiter_email_db = (recruiter.get("email") or "").strip().lower()
    if not recruiter_id:
        raise HTTPException(status_code=400, detail="Recruiter ID missing in recruiterData.")
    if not recruiter_name:
        raise HTTPException(status_code=400, detail="Recruiter name missing in recruiterData.")
    if not recruiter_phone:
        raise HTTPException(status_code=400, detail="Recruiter phone missing in recruiterData.")
    if not recruiter_email_db:
        raise HTTPException(status_code=400, detail="Recruiter email missing in recruiterData.")
    return {
        "recruiterId": recruiter_id,
        "recruiterName": recruiter_name,
        "recruiterPhone": recruiter_phone,
        "recruiterEmail": recruiter_email_db,
    }

def format_booking_confirmation_message(
    candidate_name: str,
    job_title: Optional[str],
    slot_display_text: str,
    meeting_link: Optional[str],
) -> str:
    first_name = candidate_name.split()[0] if candidate_name else "there"
    role_text = f" for the {job_title} role" if job_title else ""
    lines = [
        f"Hi {first_name}, your interview{role_text} has been confirmed.",
        "",
        f"Scheduled time: {slot_display_text}",
    ]
    if meeting_link:
        lines.extend(["", f"Meeting link: {meeting_link}"])
    lines.extend([
        "",
        "I’ve also shared the details over email.",
        "If you need to change the slot, just reply here and I’ll help you reschedule.",
    ])
    return "\n".join(lines)

def _normalize_utc_string(value: str) -> str:
    return value if value.endswith("Z") else value.replace("+00:00", "Z")

def _parse_mongo_id(slot_id: str):
    if ObjectId is None:
        return slot_id
    try:
        return ObjectId(slot_id)
    except Exception:
        return slot_id
    
async def _get_valid_google_access_token(db, coll_cal_conn: str, recruiter_email: str) -> tuple[str, str]:
    conn = await db[coll_cal_conn].find_one(
        {"recruiterEmail": recruiter_email, "provider": "google"},
        projection={"_id": 0},
    )
    if not conn:
        raise HTTPException(status_code=404, detail="No Google calendar connection found for this recruiter.")
    if conn.get("status") != "connected":
        raise HTTPException(status_code=400, detail=f"Calendar not connected (status={conn.get('status')}).")
    
    token = conn.get("token") or {}
    access_token = token.get("accessToken")
    refresh_token = token.get("refreshToken")
    expires_at = parse_dt(token.get("expiresAt"))
    calendar_id = conn.get("calendarId") or "primary"

    if (not access_token) or is_expired(expires_at):
        if not refresh_token:
            raise HTTPException(status_code=401, detail="Recruiter calendar token expired and no refresh token is available.")
        try:
            refreshed = await refresh_access_token(refresh_token)
        except httpx.HTTPError as exc:
            await db[coll_cal_conn].update_one(
                {"recruiterEmail": recruiter_email, "provider": "google"},
                {"$set": {"status": "expired", "updatedAt": datetime.now(timezone.utc)}},
            )
            raise HTTPException(status_code=401, detail=f"Token refresh failed: {str(exc)}")
        
        access_token = refreshed.get("access_token")
        if not access_token:
            raise HTTPException(status_code=401, detail="Token refresh did not return access_token.")
        
        new_expires_at = compute_expires_at(refreshed.get("expires_in"))
        new_refresh_token = refreshed.get("refresh_token") or refresh_token

        await db[coll_cal_conn].update_one(
            {"recruiterEmail": recruiter_email, "provider": "google"},
            {"$set": {
                "token.accessToken": access_token,
                "token.expiresAt": new_expires_at,
                "status": "connected",
                "updatedAt": datetime.now(timezone.utc),
            }},
        )
    return access_token, calendar_id

async def _create_google_calendar_event(
    *,
    access_token: str,
    calendar_id: str,
    recruiter_email: str,
    candidate_email: str,
    candidate_name: str,
    job_title: Optional[str],
    start_at_utc: str,
    end_at_utc: str,
    timezone_name: str,
    mode: Optional[str],
) -> dict[str, Any]:
    title = f"Interview{f' - {job_title}' if job_title else ''}"
    description_lines = [
        f"Candidate: {candidate_name}",
        f"Candidate Email: {candidate_email}",
        f"Recruiter: {recruiter_email}",
    ]
    body: dict[str, Any] = {
        "summary": title,
        "description": "\n".join(description_lines),
        "start": {"dateTime": start_at_utc.replace("Z", "+00:00"), "timeZone": timezone_name},
        "end": {"dateTime": end_at_utc.replace("Z", "+00:00"), "timeZone": timezone_name},
        "attendees": [{"email": candidate_email, "displayName": candidate_name}],
        "guestsCanModify": False,
        "guestsCanInviteOthers": False,
        "guestsCanSeeOtherGuests": True,
        "reminders": {"useDefault": False, "overrides": [{"method": "email", "minutes": 30}]}, # default reminder 30 minutes before the event for the recruiter, candidate will get reminders from our system 
    }

    params: dict[str, Any] = {"sendUpdates": "all"}

    if mode == "google_meet":
        body["conferenceData"] = {
            "createRequest": {
                "requestId": "meet_" + secrets.token_urlsafe(8),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        params["conferenceDataVersion"] = 1

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    url = GOOGLE_EVENTS_URL.format(calendar_id=calendar_id)

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, headers=headers, params=params, json=body)
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="Google authorization invalid while creating calendar event.")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"Google Calendar event creation failed: {str(exc)}")
        return resp.json()
    
def _build_email_bodies(
    *,
    candidate_name: str,
    candidate_email: str,
    recruiter_email: str,
    job_title: Optional[str],
    slot_display_text: str,
    meeting_link: Optional[str],
) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    role_text = f" for the {job_title} role" if job_title else ""
    subject = f"Interview confirmed{role_text}"

    meeting_html = f'<p><strong>Meeting link:</strong> <a href="{meeting_link}">{meeting_link}</a></p>' if meeting_link else ''
    meeting_text = f"Meeting link: {meeting_link}\n" if meeting_link else ""

    candidate_html = f"""
    <p>Hi {candidate_name},</p>
    <p>Your interview{role_text} has been confirmed.</p>
    <p><strong>Scheduled time:</strong> {slot_display_text}</p>
    {meeting_html}
    <p>If you need to reschedule, just reply on WhatsApp.</p>
    """.strip()

    candidate_text = (
        f"Hi {candidate_name},\n\n"
        f"Your interview{role_text} has been confirmed.\n"
        f"Scheduled time: {slot_display_text}\n"
        f"{meeting_text}"
        f"If you need to reschedule, just reply on WhatsApp."
    )

    recruiter_html = f"""
    <p>Hi,</p>
    <p>An interview{role_text} has been booked.</p>
    <p><strong>Candidate:</strong> {candidate_name}</p>
    <p><strong>Candidate email:</strong> {candidate_email}</p>
    <p><strong>Scheduled time:</strong> {slot_display_text}</p>
    {meeting_html}
    <p>Recruiter email: {recruiter_email}</p>
    """.strip()

    recruiter_text = (
        f"An interview{role_text} has been booked.\n"
        f"Candidate: {candidate_name}\n"
        f"Candidate email: {candidate_email}\n"
        f"Scheduled time: {slot_display_text}\n"
        f"{meeting_text}"
        f"Recruiter email: {recruiter_email}"
    )

    return (
        (subject, candidate_html, candidate_text),
        (subject, recruiter_html, recruiter_text),
    )

async def _create_reminder_docs(
    *,
    db,
    coll_interview_reminders: str,
    scheduled_interview_id: str,
    candidate_name: str,
    candidate_phone: str,
    candidate_email: str,
    recruiter_email: str,
    recruiter_phone: Optional[str],
    job_title: Optional[str],
    timezone_name: str,
    meeting_link: Optional[str],
    start_at_utc: str,
    utcnow_fn: Callable[[], Any],
) -> int:
    now = utcnow_fn()
    start_dt = datetime.fromisoformat(start_at_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
    reminder_offsets = [
        # ("24h", timedelta(hours=24)),
        # ("2h", timedelta(hours=2)),
        ("30m", timedelta(minutes=30)),
        # ("15m", timedelta(minutes=15)),
    ]

    docs: list[dict[str, Any]] = []
    role_text = f" for the {job_title} role" if job_title else ""

    for label, offset in reminder_offsets:
        send_at = start_dt - offset
        if send_at <= now:
            continue

        whatsapp_text = (
            f"Hi {candidate_name.split()[0]}, this is a reminder about your upcoming interview{role_text}. Time: {start_at_utc} ({timezone_name})."
        )
        if meeting_link:
            whatsapp_text += f" Meeting link: {meeting_link}"

        docs.append({
            "reminderId": "rem_" + secrets.token_urlsafe(12),
            "scheduledInterviewId": scheduled_interview_id,
            "channel": "whatsapp",
            "recipient": candidate_phone,
            "recipientType": "candidate",
            "sendAt": send_at,
            "status": "pending",
            "templateType": f"interview_reminder_{label}",
            "payload": {
                "candidateName": candidate_name,
                "jobTitle": job_title,
                "meetingLink": meeting_link,
                "timezone": timezone_name,
                "messageText": whatsapp_text,
            },
            "createdAt": now,
            "updatedAt": now,
        })

        # docs.append({
        #     "reminderId": "rem_" + secrets.token_urlsafe(12),
        #     "scheduledInterviewId": scheduled_interview_id,
        #     "channel": "email",
        #     "recipient": candidate_email,
        #     "recipientType": "candidate",
        #     "sendAt": send_at,
        #     "status": "pending",
        #     "templateType": f"interview_reminder_{label}",
        #     "payload": {
        #         "candidateName": candidate_name,
        #         "jobTitle": job_title,
        #         "meetingLink": meeting_link,
        #         "timezone": timezone_name,
        #     },
        #     "createdAt": now,
        #     "updatedAt": now,
        # })

        # docs.append({
        #     "reminderId": "rem_" + secrets.token_urlsafe(12),
        #     "scheduledInterviewId": scheduled_interview_id,
        #     "channel": "email",
        #     "recipient": recruiter_email,
        #     "recipientType": "recruiter",
        #     "sendAt": send_at,
        #     "status": "pending",
        #     "templateType": f"interview_reminder_{label}",
        #     "payload": {
        #         "candidateName": candidate_name,
        #         "jobTitle": job_title,
        #         "meetingLink": meeting_link,
        #         "timezone": timezone_name,
        #     },
        #     "createdAt": now,
        #     "updatedAt": now,
        # })

        if recruiter_phone:
            docs.append({
                "reminderId": "rem_" + secrets.token_urlsafe(12),
                "scheduledInterviewId": scheduled_interview_id,
                "channel": "whatsapp",
                "recipient": recruiter_phone,
                "recipientType": "recruiter",
                "sendAt": send_at,
                "status": "pending",
                "templateType": f"interview_reminder_{label}",
                "payload": {
                    "candidateName": candidate_name,
                    "jobTitle": job_title,
                    "meetingLink": meeting_link,
                    "timezone": timezone_name,
                },
                "createdAt": now,
                "updatedAt": now,
            })

    if docs:
        await db[coll_interview_reminders].insert_many(docs)
    return len(docs)

async def confirm_candidate_slot_booking_logic(
    *,
    payload: ConfirmCandidateSlotBookingRequest,
    db,
    coll_candidate_sessions: str,
    coll_avail_slots: str,
    coll_scheduled_interviews: str,
    coll_interview_reminders: str,
    coll_recruiters: str,
    coll_cal_conn: str,
    utcnow_fn: Callable[[], Any],
    send_email_fn: Callable[[str, str, str, Optional[str]], None],
    logger,
) -> ConfirmCandidateSlotBookingResponse:
    session_id = payload.sessionId.strip()
    logger.info("[confirmCandidateSlotBooking] sessionId=%s slotId=%s selectedIndex=%s", session_id, payload.slotId, payload.selectedIndex)
    session = await db[coll_candidate_sessions].find_one({"sessionId": session_id})
    if not session:
        raise HTTPException(status_code=404, detail="Scheduling session not found.")
    if session.get("status") != "active":
        raise HTTPException(status_code=400, detail="This scheduling session is no longer active.")
    if session.get("flowType") == "reschedule_request":
        raise HTTPException(status_code=400, detail="This session is part of the recruiter approval reschedule flow. Submit a reschedule request instead of booking directly.")
    if session.get("scheduledInterviewId"):
        raise HTTPException(status_code=400, detail="This scheduling session has already been booked.")

    shown_slot_ids = session.get("shownSlotIds") or []
    if payload.selectedIndex is not None:
        if payload.selectedIndex < 1 or payload.selectedIndex > len(shown_slot_ids):
            raise HTTPException(status_code=400, detail="Selected slot index is not available in this session.")
        slot_id_raw = shown_slot_ids[payload.selectedIndex - 1]
    else:
        slot_id_raw = (payload.slotId or "").strip()

    if slot_id_raw not in shown_slot_ids:
        raise HTTPException(status_code=400, detail="Selected slot does not belong to the current session. Please choose one of the shown options.")

    slot_object_id = _parse_mongo_id(slot_id_raw)
    recruiter_email = (session.get("recruiterEmail") or "").strip().lower()
    candidate_id = (session.get("candidateId") or "").strip()
    candidate_name = (session.get("candidateName") or "").strip()
    candidate_phone = (session.get("candidatePhone") or "").strip()
    candidate_email = (session.get("candidateEmail") or "").strip()
    job_id = (session.get("jobId") or "").strip()
    job_title = session.get("jobTitle")
    timezone_name = session.get("timezone") or "Asia/Kolkata"
    mode = session.get("mode") or "google_meet"
    now = utcnow_fn()

    recruiter = await _get_recruiter_doc(db, coll_recruiters, recruiter_email)

    if not candidate_email:
        raise HTTPException(status_code=400, detail="Candidate email missing in scheduling session.")
    
    held_slot = await db[coll_avail_slots].find_one_and_update(
        {
            "_id": slot_object_id,
            "recruiterEmail": recruiter_email,
            "jobId": job_id,
            "status": "active",
            "startAtUtc": {"$gt": now.strftime("%Y-%m-%dT%H:%M:%SZ")},
        },
        {
            "$set": {
                "status": "held",
                "holdCandidateId": candidate_id,
                "holdSessionId": session_id,
                "holdExpiresAt": now + timedelta(minutes=5),
                "updatedAt": now,
            }
        },
        return_document=True,
    )

    if not held_slot:
        raise HTTPException(status_code=409, detail="This slot is no longer available. Please ask for more slots.")
    
    slot_display_text = build_slot_display(held_slot, held_slot.get("timezone") or timezone_name)
    start_at_utc = held_slot["startAtUtc"]
    end_at_utc = held_slot["endAtUtc"]
    start_at_local = held_slot.get("startAtLocal")
    end_at_local = held_slot.get("endAtLocal")

    try:
        access_token, calendar_id = await _get_valid_google_access_token(db, coll_cal_conn, recruiter_email)

        event_json = await _create_google_calendar_event(
            access_token=access_token,
            calendar_id=calendar_id,
            recruiter_email=recruiter["recruiterEmail"],
            candidate_email=candidate_email,
            candidate_name=candidate_name,
            job_title=job_title,
            start_at_utc=start_at_utc,
            end_at_utc=end_at_utc,
            timezone_name=timezone_name,
            mode=mode,
        )

        meeting_link = None
        entry_points = ((event_json.get("conferenceData") or {}).get("entryPoints") or [])
        for entry in entry_points:
            if entry.get("entryPointType") == "video" and entry.get("uri"):
                meeting_link = entry["uri"]
                break
        if not meeting_link:
            meeting_link = event_json.get("hangoutLink")

        scheduled_interview_id = "int_" + secrets.token_urlsafe(16)
        interview_doc = {
            "scheduledInterviewId": scheduled_interview_id,
            "sessionId": session_id,
            "candidateId": candidate_id,
            "candidateName": candidate_name,
            "candidatePhone": candidate_phone,
            "candidateEmail": candidate_email,
            "recruiterId": recruiter["recruiterId"],
            "recruiterName": recruiter["recruiterName"],
            "recruiterPhone": recruiter["recruiterPhone"],
            "recruiterEmail": recruiter["recruiterEmail"],
            "jobId": job_id,
            "jobTitle": job_title,
            "slotId": slot_id_raw,
            "startAtUtc": start_at_utc,
            "endAtUtc": end_at_utc,
            "startAtLocal": start_at_local,
            "endAtLocal": end_at_local,
            "timezone": timezone_name,
            "mode": mode,
            "meetingLink": meeting_link,
            "calendarEventId": event_json.get("id"),
            "calendarHtmlLink": event_json.get("htmlLink"),
            "status": "scheduled",
            "createdAt": now,
            "updatedAt": now,
        }
        await db[coll_scheduled_interviews].insert_one(interview_doc)

        await db[coll_avail_slots].update_one(
            {"_id": slot_object_id, "holdSessionId": session_id, "status": "held"},
            {"$set": {
                "status": "booked",
                "bookedCandidateId": candidate_id,
                "scheduledInterviewId": scheduled_interview_id,
                "bookedAt": now,
                "updatedAt": now,
                "holdCandidateId": None,
                "holdSessionId": None,
                "holdExpiresAt": None,
            }}
        )

        await db[coll_candidate_sessions].update_one(
            {"sessionId": session_id},
            {"$set": {
                "status": "scheduled",
                "scheduledInterviewId": scheduled_interview_id,
                "selectedSlotId": slot_id_raw,
                "updatedAt": now,
            }}
        )

        reminder_count = await _create_reminder_docs(
            db=db,
            coll_interview_reminders=coll_interview_reminders,
            scheduled_interview_id=scheduled_interview_id,
            candidate_name=candidate_name,
            candidate_phone=candidate_phone,
            candidate_email=candidate_email,
            recruiter_email=recruiter["recruiterEmail"],
            recruiter_phone=recruiter["recruiterPhone"],
            job_title=job_title,
            timezone_name=timezone_name,
            meeting_link=meeting_link,
            start_at_utc=start_at_utc,
            utcnow_fn=utcnow_fn,
        )
        
        (candidate_subject, candidate_html, candidate_text), (recruiter_subject, recruiter_html, recruiter_text) = _build_email_bodies(
            candidate_name=candidate_name,
            candidate_email=candidate_email,
            recruiter_email=recruiter["recruiterEmail"],
            job_title=job_title,
            slot_display_text=slot_display_text,
            meeting_link=meeting_link,
        )
        # This will send email notifications to both candidate and recruiter about the confirmed interview slot. If email sending fails, the booking will still be successful but the error will be logged and the user can be advised to check their email connection.

        # send_email_fn(candidate_email, candidate_subject, candidate_html, candidate_text)
        send_email_fn(recruiter["recruiterEmail"], recruiter_subject, recruiter_html, recruiter_text)

    except Exception:
        logger.exception("[confirmCandidateSlotBooking] booking failed sessionId=%s slotId=%s", session_id, slot_id_raw)
        await db[coll_avail_slots].update_one(
            {"_id": slot_object_id, "holdSessionId": session_id, "status": "held"},
            {"$set": {
                "status": "active",
                "updatedAt": utcnow_fn(),
                "holdCandidateId": None,
                "holdSessionId": None,
                "holdExpiresAt": None,
            }}
        )
        raise

    message_text = format_booking_confirmation_message(
        candidate_name=candidate_name,
        job_title=job_title,
        slot_display_text=slot_display_text,
        meeting_link=meeting_link,
    )

    logger.info(
        "[confirmCandidateSlotBooking] booked sessionId=%s scheduledInterviewId=%s slotId=%s reminders=%d",
        session_id,
        scheduled_interview_id,
        slot_id_raw,
        reminder_count,
    )
    
    return ConfirmCandidateSlotBookingResponse(
        sessionId=session_id,
        candidateId=candidate_id,
        recruiterEmail=recruiter_email,
        jobId=job_id,
        jobTitle=job_title,
        scheduledInterviewId=scheduled_interview_id,
        slotId=slot_id_raw,
        meetingLink=meeting_link,
        startAtUtc=start_at_utc,
        endAtUtc=end_at_utc,
        startAtLocal=start_at_local,
        endAtLocal=end_at_local,
        messageText=message_text,
        reminderCount=reminder_count,
        message="Interview slot booked successfully.",
    )