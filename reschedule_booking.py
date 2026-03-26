from __future__ import annotations

import secrets
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Optional

from fastapi import HTTPException

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from schemas import (
    CandidateSlotOption,
    RescheduleCandidateInterviewRequest,
    RescheduleCandidateInterviewResponse,
)

# Reuse helper functions from existing files
from cancel_booking import (
    _get_valid_google_access_token,
    _delete_google_calendar_event,
    _parse_utc,
)
from candidate_scheduling import build_slot_display




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

def _parse_mongo_id(raw_id: str) -> ObjectId:
    try:
        return ObjectId(raw_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid slotId.")


def format_reschedule_slot_message(
    candidate_name: str,
    job_title: Optional[str],
    slot_options: list[CandidateSlotOption],
) -> str:
    first_name = candidate_name.split()[0] if candidate_name else "there"

    if not slot_options:
        if job_title:
            return (
                f"Hi {first_name}, I’ve cancelled your earlier interview for the {job_title} role, "
                f"but I’m not seeing any other available slots right now.\n\n"
                f"Please reply here later and I can check again."
            )
        return (
            f"Hi {first_name}, I’ve cancelled your earlier interview, "
            f"but I’m not seeing any other available slots right now.\n\n"
            f"Please reply here later and I can check again."
        )

    lines = [f"Hi {first_name}, no problem — I’ve cancelled your earlier interview.", ""]

    if job_title:
        lines.append(f"Here are a few other available slots for the {job_title} role:")
    else:
        lines.append("Here are a few other available slots:")

    lines.append("")

    for idx, slot in enumerate(slot_options, start=1):
        lines.append(f"{idx}. {slot.displayText}")

    lines.append("")
    lines.append("You can reply with 1, 2, or 3 to choose a slot.")
    lines.append('If these don’t work, reply with "more slots".')

    return "\n".join(lines)


async def reschedule_candidate_interview_logic(
    *,
    payload: RescheduleCandidateInterviewRequest,
    db,
    coll_scheduled_interviews: str,
    coll_interview_reminders: str,
    coll_avail_slots: str,
    coll_candidate_sessions: str,
    coll_recruiters: str,
    coll_cal_conn: str,
    utcnow_fn: Callable[[], Any],
    logger,
) -> RescheduleCandidateInterviewResponse:
    scheduled_interview_id = payload.scheduledInterviewId.strip()
    requested_by = payload.requestedBy.strip() or "candidate"
    now = utcnow_fn()

    logger.info("[rescheduleCandidateInterview] scheduledInterviewId=%s", scheduled_interview_id)

    interview = await db[coll_scheduled_interviews].find_one({
        "scheduledInterviewId": scheduled_interview_id
    })
    if not interview:
        raise HTTPException(status_code=404, detail="Scheduled interview not found.")

    current_status = (interview.get("status") or "").strip().lower()
    if current_status == "cancelled":
        raise HTTPException(status_code=400, detail="Interview is already cancelled.")
    if current_status == "rescheduled":
        raise HTTPException(status_code=400, detail="Interview is already rescheduled.")
    if current_status != "scheduled":
        raise HTTPException(status_code=400, detail="Only scheduled interviews can be rescheduled.")

    candidate_id = (interview.get("candidateId") or "").strip()
    candidate_name = (interview.get("candidateName") or "").strip()
    candidate_phone = (interview.get("candidatePhone") or "").strip()
    candidate_email = (interview.get("candidateEmail") or "").strip()
    recruiter_email = (interview.get("recruiterEmail") or "").strip().lower()
    job_id = (interview.get("jobId") or "").strip()
    job_title = interview.get("jobTitle")
    slot_id = (interview.get("slotId") or "").strip()
    calendar_event_id = interview.get("calendarEventId")
    start_at_utc = interview.get("startAtUtc")
    timezone_name = interview.get("timezone") or "Asia/Kolkata"
    provider = interview.get("provider") or "google"
    mode = interview.get("mode") or "google_meet"
    previous_session_id = (interview.get("sessionId") or "").strip() or None

    recruiter = await _get_recruiter_doc(db, coll_recruiters, recruiter_email)

    # 1) cancel old calendar event
    if calendar_event_id:
        access_token, calendar_id = await _get_valid_google_access_token(db, coll_cal_conn, recruiter_email)
        await _delete_google_calendar_event(
            access_token=access_token,
            calendar_id=calendar_id,
            event_id=calendar_event_id,
        )

    # 2) mark interview as rescheduled
    await db[coll_scheduled_interviews].update_one(
        {"scheduledInterviewId": scheduled_interview_id},
        {"$set": {
            "status": "rescheduled",
            "rescheduledBy": requested_by,
            "rescheduledAt": now,
            "updatedAt": now,
        }}
    )

    # 3) cancel pending reminders
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

    # 4) reopen old slot if still in future
    old_slot_reopened = False
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
                old_slot_reopened = reopen_result.modified_count == 1
        except Exception:
            logger.exception(
                "[rescheduleCandidateInterview] failed to reopen old slot scheduledInterviewId=%s",
                scheduled_interview_id,
            )

    # 5) close any current active session for this candidate/job before creating a new one
    close_session_filter = {
        "candidateId": candidate_id,
        "recruiterEmail": recruiter_email,
        "jobId": job_id,
        "status": "active",
    }
    if previous_session_id:
        close_session_filter["sessionId"] = previous_session_id

    await db[coll_candidate_sessions].update_many(
        close_session_filter,
        {"$set": {
            "status": "rescheduled",
            "updatedAt": now,
            "expiresAt": now,
        }}
    )

    # 6) create fresh scheduling session only if replacement slots exist
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    raw_slots = await db[coll_avail_slots].find(
        {
            "recruiterEmail": recruiter_email,
            "jobId": job_id,
            "status": "active",
            "startAtUtc": {"$gt": now_iso},
        }
    ).sort("startAtUtc", 1).limit(3).to_list(length=3)

    slot_options: list[CandidateSlotOption] = []
    shown_slot_ids: list[str] = []

    for slot in raw_slots:
        slot_id_str = str(slot["_id"])
        shown_slot_ids.append(slot_id_str)
        slot_options.append(
            CandidateSlotOption(
                slotId=slot_id_str,
                displayText=build_slot_display(slot, slot.get("timezone") or timezone_name),
                startAtUtc=slot.get("startAtUtc"),
                endAtUtc=slot.get("endAtUtc"),
                startAtLocal=slot.get("startAtLocal"),
                endAtLocal=slot.get("endAtLocal"),
            )
        )

    new_session_id = None
    has_more = len(slot_options) == 3

    if slot_options:
        new_session_id = "sess_" + secrets.token_urlsafe(16)
        new_session_doc = {
            "sessionId": new_session_id,
            "candidateId": candidate_id,
            "candidateName": candidate_name,
            "candidatePhone": candidate_phone,
            "candidateEmail": candidate_email,
            "recruiterId": recruiter.get("recruiterId"),
            "recruiterName": recruiter.get("recruiterName"),
            "recruiterPhone": recruiter.get("recruiterPhone"),
            "recruiterEmail": recruiter.get("recruiterEmail") or recruiter_email,
            "jobId": job_id,
            "jobTitle": job_title,
            "provider": provider,
            "timezone": timezone_name,
            "mode": mode,
            "status": "active",
            "shownSlotIds": shown_slot_ids,
            "lastShownStartAtUtc": slot_options[-1].startAtUtc if slot_options else None,
            "scheduledInterviewId": None,
            "rescheduledFromInterviewId": scheduled_interview_id,
            "createdAt": now,
            "updatedAt": now,
            "expiresAt": now + timedelta(hours=24),
        }
        try:
            await db[coll_candidate_sessions].insert_one(new_session_doc)
        except DuplicateKeyError:
            existing_session = await db[coll_candidate_sessions].find_one(
                {
                    "candidateId": candidate_id,
                    "recruiterEmail": recruiter_email,
                    "jobId": job_id,
                    "status": "active",
                },
                sort=[("updatedAt", -1)],
            )
            if not existing_session:
                raise HTTPException(
                    status_code=409,
                    detail="Unable to create a replacement scheduling session. Please try again.",
                )

            existing_session_created_at = existing_session.get("createdAt")
            existing_session_source = (existing_session.get("rescheduledFromInterviewId") or "").strip()
            recently_created = False
            if isinstance(existing_session_created_at, datetime):
                recently_created = (now - existing_session_created_at) <= timedelta(minutes=2)

            if existing_session_source == scheduled_interview_id and recently_created:
                new_session_id = existing_session["sessionId"]
            else:
                raise HTTPException(
                    status_code=409,
                    detail="Another active scheduling session already exists for this candidate and job.",
                )

    message_text = format_reschedule_slot_message(candidate_name, job_title, slot_options)

    logger.info(
        "[rescheduleCandidateInterview] oldInterview=%s newSessionId=%s slots=%d reopened=%s remindersCancelled=%d",
        scheduled_interview_id,
        new_session_id,
        len(slot_options),
        old_slot_reopened,
        cancelled_reminders_result.modified_count,
    )

    return RescheduleCandidateInterviewResponse(
        oldScheduledInterviewId=scheduled_interview_id,
        newSessionId=new_session_id,
        candidateId=candidate_id,
        recruiterEmail=recruiter_email,
        jobId=job_id,
        jobTitle=job_title,
        oldSlotReopened=old_slot_reopened,
        cancelledReminderCount=cancelled_reminders_result.modified_count,
        slots=slot_options,
        messageText=message_text,
        hasMore=has_more,
        nextAction="show_slots" if slot_options else "no_slots_available",
        availableActions=["select_slot", "more_slots"] if slot_options else ["check_later"],
        message="Interview rescheduling started successfully." if slot_options else "Interview cancelled. No replacement slots are available right now.",
    )