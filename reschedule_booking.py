from __future__ import annotations

import secrets
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Optional

from fastapi import HTTPException
from bson import ObjectId

from schemas import (
    CandidateSlotOption,
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
    RescheduleRequestDashboardItem,
)

from cancel_booking import (
    _get_valid_google_access_token,
    _delete_google_calendar_event,
    _parse_utc,
)
from candidate_scheduling import build_slot_display
from confirm_booking import (
    _create_google_calendar_event,
    _create_reminder_docs,
    _build_email_bodies,
)

SESSION_TTL_HOURS = 24


def _session_expiry(now):
    return now + timedelta(hours=SESSION_TTL_HOURS)


def _parse_mongo_id(raw_id: str) -> ObjectId:
    try:
        return ObjectId(raw_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid slotId.")


def _format_interview_time(doc: dict[str, Any]) -> str:
    start_local = doc.get("startAtLocal")
    end_local = doc.get("endAtLocal")
    timezone_name = doc.get("timezone") or "Asia/Kolkata"
    if start_local and end_local:
        return f"{start_local} to {end_local} ({timezone_name})"
    if doc.get("startAtUtc") and doc.get("endAtUtc"):
        return f"{doc['startAtUtc']} to {doc['endAtUtc']}"
    if doc.get("startAtUtc"):
        return str(doc["startAtUtc"])
    return "the currently scheduled time"


def _build_reschedule_rejection_email(
    *,
    candidate_name: str,
    job_title: Optional[str],
    original_slot_text: str,
    requested_slot_text: Optional[str],
    reason: Optional[str],
) -> tuple[str, str, str]:
    role_text = f" for the {job_title} role" if job_title else ""
    subject = f"Reschedule request update{role_text}"
    first_name = candidate_name.split()[0] if candidate_name else "there"
    requested_html = (
        f"<p><strong>Requested time:</strong> {requested_slot_text}</p>"
        if requested_slot_text
        else ""
    )
    requested_text = f"Requested time: {requested_slot_text}\n" if requested_slot_text else ""
    reason_html = f"<p><strong>Reason:</strong> {reason}</p>" if reason else ""
    reason_text = f"Reason: {reason}\n" if reason else ""

    html_body = f"""
    <p>Hi {first_name},</p>
    <p>Your reschedule request{role_text} was not approved.</p>
    <p>Your current interview remains scheduled at the original time.</p>
    <p><strong>Current interview time:</strong> {original_slot_text}</p>
    {requested_html}
    {reason_html}
    <p>If you still need a different time, please reply on WhatsApp and we can help you request another slot.</p>
    """.strip()

    text_body = (
        f"Hi {first_name},\n\n"
        f"Your reschedule request{role_text} was not approved.\n"
        f"Your current interview remains scheduled at the original time.\n"
        f"Current interview time: {original_slot_text}\n"
        f"{requested_text}"
        f"{reason_text}"
        "If you still need a different time, please reply on WhatsApp and we can help you request another slot."
    )

    return subject, html_body, text_body


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


async def _create_reschedule_session(
    *,
    db,
    coll_candidate_sessions: str,
    coll_recruiters: str,
    coll_avail_slots: str,
    coll_scheduled_interviews: str,
    interview: dict[str, Any],
    now,
):
    candidate_id = (interview.get("candidateId") or "").strip()
    candidate_name = (interview.get("candidateName") or "").strip()
    candidate_phone = (interview.get("candidatePhone") or "").strip()
    candidate_email = (interview.get("candidateEmail") or "").strip()
    recruiter_email = (interview.get("recruiterEmail") or "").strip().lower()
    job_id = (interview.get("jobId") or "").strip()
    job_title = interview.get("jobTitle")
    timezone_name = interview.get("timezone") or "Asia/Kolkata"
    provider = interview.get("provider") or "google"
    mode = interview.get("mode") or "google_meet"
    scheduled_interview_id = (interview.get("scheduledInterviewId") or "").strip()
    current_slot_id = (interview.get("slotId") or "").strip()

    recruiter = await _get_recruiter_doc(db, coll_recruiters, recruiter_email)

    current_slot_oid = None
    try:
        current_slot_oid = _parse_mongo_id(current_slot_id) if current_slot_id else None
    except HTTPException:
        current_slot_oid = None

    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    query: dict[str, Any] = {
        "recruiterEmail": recruiter_email,
        "status": "active",
        "startAtUtc": {"$gt": now_iso},
    }
    if current_slot_oid is not None:
        query["_id"] = {"$ne": current_slot_oid}

    raw_slots = await db[coll_avail_slots].find(query).sort("startAtUtc", 1).limit(3).to_list(length=3)

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

    await db[coll_candidate_sessions].update_many(
        {
            "candidateId": candidate_id,
            "recruiterEmail": recruiter_email,
            "jobId": job_id,
            "status": "active",
        },
        {"$set": {"status": "replaced", "updatedAt": now, "expiresAt": now}},
    )

    new_session_id = None
    if slot_options:
        new_session_id = "sess_" + secrets.token_urlsafe(16)
        await db[coll_candidate_sessions].insert_one(
            {
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
                "flowType": "reschedule_request",
                "rescheduleRequestState": "awaiting_slot_selection",
                "pendingRescheduleInterviewId": scheduled_interview_id,
                "shownSlotIds": shown_slot_ids,
                "seenSlotIds": shown_slot_ids.copy(),
                "lastShownStartAtUtc": slot_options[-1].startAtUtc if slot_options else None,
                "scheduledInterviewId": scheduled_interview_id,
                "createdAt": now,
                "updatedAt": now,
                "expiresAt": _session_expiry(now),
            }
        )

    return new_session_id, slot_options, len(slot_options) == 3


async def start_candidate_reschedule_request_logic(
    *,
    payload: StartCandidateRescheduleRequest,
    db,
    coll_scheduled_interviews: str,
    coll_candidate_sessions: str,
    coll_recruiters: str,
    coll_avail_slots: str,
    coll_reschedule_requests: str,
    utcnow_fn: Callable[[], Any],
    logger,
) -> StartCandidateRescheduleRequestResponse:
    scheduled_interview_id = payload.scheduledInterviewId.strip()
    now = utcnow_fn()

    interview = await db[coll_scheduled_interviews].find_one({"scheduledInterviewId": scheduled_interview_id})
    if not interview:
        raise HTTPException(status_code=404, detail="Scheduled interview not found.")

    current_status = (interview.get("status") or "").strip().lower()
    if current_status not in {"scheduled", "reschedule_requested"}:
        raise HTTPException(status_code=400, detail="Only scheduled interviews can be rescheduled.")

    pending_request = await db[coll_reschedule_requests].find_one(
        {"scheduledInterviewId": scheduled_interview_id, "requestStatus": "pending"},
        sort=[("createdAt", -1)],
    )
    if pending_request:
        raise HTTPException(status_code=409, detail="A reschedule request is already pending recruiter approval.")

    new_session_id, slot_options, has_more = await _create_reschedule_session(
        db=db,
        coll_candidate_sessions=coll_candidate_sessions,
        coll_recruiters=coll_recruiters,
        coll_avail_slots=coll_avail_slots,
        coll_scheduled_interviews=coll_scheduled_interviews,
        interview=interview,
        now=now,
    )

    candidate_name = (interview.get("candidateName") or "").strip() or "there"
    job_title = interview.get("jobTitle")
    recruiter_email = (interview.get("recruiterEmail") or "").strip().lower()
    candidate_id = (interview.get("candidateId") or "").strip()
    job_id = (interview.get("jobId") or "").strip()

    if slot_options:
        lines = [
            f"Hi {candidate_name.split()[0]}, thanks for confirming.",
            "Your current interview will stay unchanged unless the recruiter approves this reschedule request.",
            "",
            f"Here are the available alternative slots{f' for the {job_title} role' if job_title else ''}:",
            "",
        ]
        for idx, slot in enumerate(slot_options, start=1):
            lines.append(f"{idx}. {slot.displayText}")
        lines.append("")
        lines.append("Reply with 1, 2, or 3 to submit a reschedule request for one of these slots.")
        lines.append('If these don’t work, reply with "more slots".')
        message_text = "\n".join(lines)
        next_action = "show_slots"
        actions = ["select_slot", "more_slots"]
        message = "Reschedule request flow started successfully."
    else:
        message_text = (
            f"Hi {candidate_name.split()[0]}, I can initiate a reschedule request, but I’m not seeing any alternate slots right now. "
            f"Please check again later."
        )
        next_action = "no_slots_available"
        actions = ["check_later"]
        message = "No alternative slots available right now."

    logger.info("[startCandidateRescheduleRequest] scheduledInterviewId=%s sessionId=%s slots=%d", scheduled_interview_id, new_session_id, len(slot_options))

    return StartCandidateRescheduleRequestResponse(
        oldScheduledInterviewId=scheduled_interview_id,
        sessionId=new_session_id,
        candidateId=candidate_id,
        recruiterEmail=recruiter_email,
        jobId=job_id,
        jobTitle=job_title,
        slots=slot_options,
        messageText=message_text,
        hasMore=has_more,
        nextAction=next_action,
        availableActions=actions,
        message=message,
    )


async def create_candidate_reschedule_request_logic(
    *,
    payload: CreateCandidateRescheduleRequestRequest,
    db,
    coll_candidate_sessions: str,
    coll_scheduled_interviews: str,
    coll_avail_slots: str,
    coll_reschedule_requests: str,
    utcnow_fn: Callable[[], Any],
    logger,
) -> CreateCandidateRescheduleRequestResponse:
    session_id = payload.sessionId.strip()
    now = utcnow_fn()

    session = await db[coll_candidate_sessions].find_one({"sessionId": session_id})
    if not session:
        raise HTTPException(status_code=404, detail="Scheduling session not found.")
    if session.get("status") != "active":
        raise HTTPException(status_code=400, detail="This reschedule session is no longer active.")
    if session.get("flowType") != "reschedule_request":
        raise HTTPException(status_code=400, detail="This session is not for reschedule approval flow.")

    shown_slot_ids = session.get("shownSlotIds") or []
    if payload.selectedIndex is not None:
        if payload.selectedIndex < 1 or payload.selectedIndex > len(shown_slot_ids):
            raise HTTPException(status_code=400, detail="Selected slot index is not available in this session.")
        slot_id_raw = shown_slot_ids[payload.selectedIndex - 1]
    else:
        slot_id_raw = (payload.slotId or "").strip()

    if slot_id_raw not in shown_slot_ids:
        raise HTTPException(status_code=400, detail="Selected slot does not belong to the current session.")

    scheduled_interview_id = (session.get("pendingRescheduleInterviewId") or session.get("scheduledInterviewId") or "").strip()
    if not scheduled_interview_id:
        raise HTTPException(status_code=400, detail="Reschedule interview context missing in session.")

    interview = await db[coll_scheduled_interviews].find_one({"scheduledInterviewId": scheduled_interview_id})
    if not interview:
        raise HTTPException(status_code=404, detail="Original scheduled interview not found.")
    if (interview.get("status") or "").strip().lower() not in {"scheduled", "reschedule_requested"}:
        raise HTTPException(status_code=400, detail="This interview can no longer accept a reschedule request.")

    existing_pending = await db[coll_reschedule_requests].find_one(
        {"scheduledInterviewId": scheduled_interview_id, "requestStatus": "pending"}
    )
    if existing_pending:
        raise HTTPException(status_code=409, detail="A reschedule request is already pending recruiter approval.")

    slot = await db[coll_avail_slots].find_one(
        {
            "_id": _parse_mongo_id(slot_id_raw),
            "recruiterEmail": (session.get("recruiterEmail") or "").strip().lower(),
            "status": "active",
        }
    )
    if not slot:
        raise HTTPException(status_code=409, detail="This requested slot is no longer available. Please ask for more slots.")

    request_id = "req_" + secrets.token_urlsafe(16)
    slot_display_text = build_slot_display(slot, slot.get("timezone") or (session.get("timezone") or "Asia/Kolkata"))
    request_doc = {
        "requestId": request_id,
        "scheduledInterviewId": scheduled_interview_id,
        "sessionId": session_id,
        "candidateId": session.get("candidateId"),
        "candidateName": session.get("candidateName"),
        "candidatePhone": session.get("candidatePhone"),
        "candidateEmail": session.get("candidateEmail"),
        "recruiterId": session.get("recruiterId"),
        "recruiterEmail": (session.get("recruiterEmail") or "").strip().lower(),
        "jobId": session.get("jobId"),
        "jobTitle": session.get("jobTitle"),
        "currentSlotId": interview.get("slotId"),
        "requestedSlotId": slot_id_raw,
        "requestedSlotDisplayText": slot_display_text,
        "requestedSlotSnapshot": {
            "startAtUtc": slot.get("startAtUtc"),
            "endAtUtc": slot.get("endAtUtc"),
            "startAtLocal": slot.get("startAtLocal"),
            "endAtLocal": slot.get("endAtLocal"),
            "timezone": slot.get("timezone") or (session.get("timezone") or "Asia/Kolkata"),
        },
        "requestSource": "candidate_whatsapp",
        "requestedBy": "candidate",
        "approvalStatus": "pending",
        "requestStatus": "pending",
        "requestedAt": now,
        "reviewedAt": None,
        "reviewedBy": None,
        "reviewComment": None,
        "createdAt": now,
        "updatedAt": now,
    }
    await db[coll_reschedule_requests].insert_one(request_doc)

    await db[coll_scheduled_interviews].update_one(
        {"scheduledInterviewId": scheduled_interview_id, "status": {"$in": ["scheduled", "reschedule_requested"]}},
        {"$set": {"status": "reschedule_requested", "updatedAt": now}},
    )
    await db[coll_candidate_sessions].update_one(
        {"sessionId": session_id},
        {
            "$set": {
                "status": "request_pending",
                "rescheduleRequestState": "request_submitted",
                "pendingRequestedSlotId": slot_id_raw,
                "pendingRescheduleRequestId": request_id,
                "updatedAt": now,
                "expiresAt": _session_expiry(now),
            }
        },
    )

    message_text = (
        f"Thanks {((session.get('candidateName') or '').split() or ['there'])[0]}. "
        f"Your reschedule request for {slot_display_text} has been submitted. "
        f"Your current interview will remain unchanged until the recruiter reviews this request."
    )

    logger.info("[createCandidateRescheduleRequest] requestId=%s scheduledInterviewId=%s slotId=%s", request_id, scheduled_interview_id, slot_id_raw)

    return CreateCandidateRescheduleRequestResponse(
        requestId=request_id,
        sessionId=session_id,
        oldScheduledInterviewId=scheduled_interview_id,
        candidateId=(session.get("candidateId") or "").strip(),
        recruiterEmail=(session.get("recruiterEmail") or "").strip().lower(),
        jobId=(session.get("jobId") or "").strip(),
        jobTitle=session.get("jobTitle"),
        requestedSlotId=slot_id_raw,
        requestedSlotDisplayText=slot_display_text,
        requestStatus="pending",
        messageText=message_text,
        availableActions=["wait_for_recruiter"],
        message="Reschedule request submitted successfully.",
    )


async def approve_reschedule_request_logic(
    *,
    payload: ApproveRescheduleRequestRequest,
    db,
    coll_reschedule_requests: str,
    coll_scheduled_interviews: str,
    coll_interview_reminders: str,
    coll_avail_slots: str,
    coll_candidate_sessions: str,
    coll_recruiters: str,
    coll_cal_conn: str,
    coll_interview_reminders_out: str,
    utcnow_fn: Callable[[], Any],
    send_email_fn: Callable[[str, str, str, Optional[str]], None],
    logger,
) -> ApproveRescheduleRequestResponse:
    request_id = payload.requestId.strip()
    reviewed_by = payload.reviewedBy.strip()
    now = utcnow_fn()

    request_doc = await db[coll_reschedule_requests].find_one({"requestId": request_id})
    if not request_doc:
        raise HTTPException(status_code=404, detail="Reschedule request not found.")
    if request_doc.get("requestStatus") != "pending":
        raise HTTPException(status_code=400, detail="Only pending requests can be approved.")

    scheduled_interview_id = request_doc["scheduledInterviewId"]
    interview = await db[coll_scheduled_interviews].find_one({"scheduledInterviewId": scheduled_interview_id})
    if not interview:
        raise HTTPException(status_code=404, detail="Original scheduled interview not found.")
    if (interview.get("status") or "").strip().lower() not in {"scheduled", "reschedule_requested"}:
        raise HTTPException(status_code=400, detail="Original interview is not in an approvable state.")

    requested_slot_id = request_doc["requestedSlotId"]
    old_session_id = (interview.get("sessionId") or "").strip() or None
    new_session_id = (request_doc.get("sessionId") or "").strip() or None
    slot_oid = _parse_mongo_id(requested_slot_id)
    held_slot = await db[coll_avail_slots].find_one_and_update(
        {
            "_id": slot_oid,
            "recruiterEmail": (request_doc.get("recruiterEmail") or "").strip().lower(),
            "status": "active",
            "startAtUtc": {"$gt": now.strftime("%Y-%m-%dT%H:%M:%SZ")},
        },
        {
            "$set": {
                "status": "held",
                "holdCandidateId": request_doc.get("candidateId"),
                "holdSessionId": request_doc.get("sessionId"),
                "holdExpiresAt": now + timedelta(minutes=5),
                "updatedAt": now,
            }
        },
        return_document=True,
    )
    if not held_slot:
        raise HTTPException(status_code=409, detail="Requested slot is no longer available.")

    recruiter_email = (request_doc.get("recruiterEmail") or "").strip().lower()
    recruiter = await _get_recruiter_doc(db, coll_recruiters, recruiter_email)
    access_token, calendar_id = await _get_valid_google_access_token(db, coll_cal_conn, recruiter_email)

    old_event_id = interview.get("calendarEventId")
    if old_event_id:
        await _delete_google_calendar_event(access_token=access_token, calendar_id=calendar_id, event_id=old_event_id)

    start_at_utc = held_slot["startAtUtc"]
    end_at_utc = held_slot["endAtUtc"]
    timezone_name = request_doc.get("requestedSlotSnapshot", {}).get("timezone") or interview.get("timezone") or "Asia/Kolkata"

    event_json = await _create_google_calendar_event(
        access_token=access_token,
        calendar_id=calendar_id,
        recruiter_email=recruiter["recruiterEmail"],
        candidate_email=request_doc.get("candidateEmail") or interview.get("candidateEmail") or "",
        candidate_name=request_doc.get("candidateName") or interview.get("candidateName") or "Candidate",
        job_title=request_doc.get("jobTitle") or interview.get("jobTitle"),
        start_at_utc=start_at_utc,
        end_at_utc=end_at_utc,
        timezone_name=timezone_name,
        mode=interview.get("mode") or "google_meet",
    )

    meeting_link = None
    entry_points = ((event_json.get("conferenceData") or {}).get("entryPoints") or [])
    for entry in entry_points:
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            meeting_link = entry["uri"]
            break
    if not meeting_link:
        meeting_link = event_json.get("hangoutLink")

    new_scheduled_interview_id = "int_" + secrets.token_urlsafe(16)
    await db[coll_scheduled_interviews].insert_one(
        {
            "scheduledInterviewId": new_scheduled_interview_id,
            "sessionId": new_session_id,
            "candidateId": request_doc.get("candidateId"),
            "candidateName": request_doc.get("candidateName"),
            "candidatePhone": request_doc.get("candidatePhone"),
            "candidateEmail": request_doc.get("candidateEmail"),
            "recruiterId": recruiter.get("recruiterId"),
            "recruiterName": recruiter.get("recruiterName"),
            "recruiterPhone": recruiter.get("recruiterPhone"),
            "recruiterEmail": recruiter.get("recruiterEmail"),
            "jobId": request_doc.get("jobId"),
            "jobTitle": request_doc.get("jobTitle"),
            "slotId": requested_slot_id,
            "startAtUtc": start_at_utc,
            "endAtUtc": end_at_utc,
            "startAtLocal": held_slot.get("startAtLocal"),
            "endAtLocal": held_slot.get("endAtLocal"),
            "timezone": timezone_name,
            "mode": interview.get("mode") or "google_meet",
            "meetingLink": meeting_link,
            "calendarEventId": event_json.get("id"),
            "calendarHtmlLink": event_json.get("htmlLink"),
            "status": "scheduled",
            "rescheduledFromInterviewId": scheduled_interview_id,
            "createdAt": now,
            "updatedAt": now,
        }
    )

    await db[coll_interview_reminders].update_many(
        {"scheduledInterviewId": scheduled_interview_id, "status": "pending"},
        {"$set": {"status": "cancelled", "updatedAt": now}},
    )

    await db[coll_avail_slots].update_one(
        {"_id": slot_oid, "status": "held", "holdSessionId": request_doc.get("sessionId")},
        {"$set": {
            "status": "booked",
            "bookedCandidateId": request_doc.get("candidateId"),
            "scheduledInterviewId": new_scheduled_interview_id,
            "bookedAt": now,
            "updatedAt": now,
            "holdCandidateId": None,
            "holdSessionId": None,
            "holdExpiresAt": None,
        }}
    )

    old_slot_id = (interview.get("slotId") or "").strip()
    if old_slot_id:
        try:
            old_slot_oid = _parse_mongo_id(old_slot_id)
            await db[coll_avail_slots].update_one(
                {"_id": old_slot_oid, "scheduledInterviewId": scheduled_interview_id, "status": "booked"},
                {"$set": {
                    "status": "active",
                    "bookedCandidateId": None,
                    "scheduledInterviewId": None,
                    "bookedAt": None,
                    "updatedAt": now,
                }}
            )
        except Exception:
            logger.exception("[approveRescheduleRequest] failed reopening old slot for interview=%s", scheduled_interview_id)

    await db[coll_scheduled_interviews].update_one(
        {"scheduledInterviewId": scheduled_interview_id},
        {"$set": {
            "status": "rescheduled",
            "rescheduledAt": now,
            "rescheduleApprovedAt": now,
            "updatedAt": now,
        }}
    )

    reminder_count = await _create_reminder_docs(
        db=db,
        coll_interview_reminders=coll_interview_reminders_out,
        scheduled_interview_id=new_scheduled_interview_id,
        candidate_name=request_doc.get("candidateName") or interview.get("candidateName") or "Candidate",
        candidate_phone=request_doc.get("candidatePhone") or interview.get("candidatePhone") or "",
        candidate_email=request_doc.get("candidateEmail") or interview.get("candidateEmail") or "",
        recruiter_email=recruiter.get("recruiterEmail") or recruiter_email,
        recruiter_phone=recruiter.get("recruiterPhone"),
        job_title=request_doc.get("jobTitle") or interview.get("jobTitle"),
        timezone_name=timezone_name,
        meeting_link=meeting_link,
        start_at_utc=start_at_utc,
        utcnow_fn=utcnow_fn,
    )

    slot_display_text = build_slot_display(held_slot, timezone_name)
    (candidate_email_payload, recruiter_email_payload) = _build_email_bodies(
        candidate_name=request_doc.get("candidateName") or interview.get("candidateName") or "Candidate",
        candidate_email=request_doc.get("candidateEmail") or interview.get("candidateEmail") or "",
        recruiter_email=recruiter.get("recruiterEmail") or recruiter_email,
        job_title=request_doc.get("jobTitle") or interview.get("jobTitle"),
        slot_display_text=slot_display_text,
        meeting_link=meeting_link,
    )
    try:
        cand_subject, cand_html, cand_text = candidate_email_payload
        rec_subject, rec_html, rec_text = recruiter_email_payload
        candidate_email = request_doc.get("candidateEmail") or interview.get("candidateEmail")
        if candidate_email:
            send_email_fn(candidate_email, cand_subject, cand_text, cand_html)
        send_email_fn(recruiter.get("recruiterEmail") or recruiter_email, rec_subject, rec_text, rec_html)
    except Exception:
        logger.exception("[approveRescheduleRequest] email send failed requestId=%s", request_id)

    await db[coll_reschedule_requests].update_one(
        {"requestId": request_id},
        {"$set": {
            "requestStatus": "approved",
            "approvalStatus": "approved",
            "reviewedAt": now,
            "reviewedBy": reviewed_by,
            "updatedAt": now,
            "approvedScheduledInterviewId": new_scheduled_interview_id,
            "reminderCount": reminder_count,
        }}
    )

    if new_session_id:
        await db[coll_candidate_sessions].update_one(
            {"sessionId": new_session_id},
            {"$set": {
                "status": "scheduled",
                "scheduledInterviewId": new_scheduled_interview_id,
                "rescheduleRequestState": "approved",
                "flowType": None,
                "pendingRescheduleRequestId": None,
                "pendingRequestedSlotId": None,
                "updatedAt": now,
                "expiresAt": _session_expiry(now),
                "lastAgentContext.scheduledInterviewId": new_scheduled_interview_id,
                "lastAgentContext.activeSessionId": None,
                "lastAgentContext.flowType": None,
                "lastAgentContext.rescheduleRequestState": None,
                "lastAgentContext.pendingRescheduleRequestId": None,
            }}
        )

    # The approved reschedule session becomes the canonical thread. Remove the
    # previous booked-session document to avoid ambiguity in later WhatsApp turns.
    if old_session_id and new_session_id and old_session_id != new_session_id:
        await db[coll_candidate_sessions].delete_one({"sessionId": old_session_id})

    message_text = (
        f"Reschedule request approved. The old interview was replaced with the requested slot {slot_display_text}. "
        f"A new calendar event, reminders, and emails have been created."
    )

    return ApproveRescheduleRequestResponse(
        requestId=request_id,
        oldScheduledInterviewId=scheduled_interview_id,
        newScheduledInterviewId=new_scheduled_interview_id,
        candidateId=(request_doc.get("candidateId") or "").strip(),
        recruiterEmail=recruiter.get("recruiterEmail") or recruiter_email,
        jobId=(request_doc.get("jobId") or "").strip(),
        jobTitle=request_doc.get("jobTitle"),
        approved=True,
        messageText=message_text,
        message="Reschedule request approved successfully.",
    )


async def reject_reschedule_request_logic(
    *,
    payload: RejectRescheduleRequestRequest,
    db,
    coll_reschedule_requests: str,
    coll_scheduled_interviews: str,
    coll_candidate_sessions: str,
    utcnow_fn: Callable[[], Any],
    send_email_fn: Callable[[str, str, str, Optional[str]], None],
    logger,
) -> RejectRescheduleRequestResponse:
    request_id = payload.requestId.strip()
    reviewed_by = payload.reviewedBy.strip()
    reason = (payload.reason or "").strip() or None
    now = utcnow_fn()

    request_doc = await db[coll_reschedule_requests].find_one({"requestId": request_id})
    if not request_doc:
        raise HTTPException(status_code=404, detail="Reschedule request not found.")
    if request_doc.get("requestStatus") != "pending":
        raise HTTPException(status_code=400, detail="Only pending requests can be rejected.")

    scheduled_interview_id = request_doc["scheduledInterviewId"]
    interview = await db[coll_scheduled_interviews].find_one(
        {"scheduledInterviewId": scheduled_interview_id},
        projection={"_id": 0},
    )
    await db[coll_reschedule_requests].update_one(
        {"requestId": request_id},
        {"$set": {
            "requestStatus": "rejected",
            "approvalStatus": "rejected",
            "reviewedAt": now,
            "reviewedBy": reviewed_by,
            "reviewComment": reason,
            "updatedAt": now,
        }}
    )
    await db[coll_scheduled_interviews].update_one(
        {"scheduledInterviewId": scheduled_interview_id, "status": "reschedule_requested"},
        {"$set": {"status": "scheduled", "updatedAt": now}}
    )
    if request_doc.get("sessionId"):
        await db[coll_candidate_sessions].update_one(
            {"sessionId": request_doc.get("sessionId")},
            {"$set": {"status": "closed", "rescheduleRequestState": "rejected", "updatedAt": now, "expiresAt": now}}
        )

    candidate_email = (request_doc.get("candidateEmail") or (interview or {}).get("candidateEmail") or "").strip()
    if candidate_email:
        original_slot_text = _format_interview_time(interview or {})
        subject, html_body, text_body = _build_reschedule_rejection_email(
            candidate_name=request_doc.get("candidateName") or (interview or {}).get("candidateName") or "there",
            job_title=request_doc.get("jobTitle") or (interview or {}).get("jobTitle"),
            original_slot_text=original_slot_text,
            requested_slot_text=request_doc.get("requestedSlotDisplayText"),
            reason=reason,
        )
        try:
            send_email_fn(candidate_email, subject, html_body, text_body)
        except Exception:
            logger.exception("[rejectRescheduleRequest] candidate email send failed requestId=%s", request_id)

    message_text = (
        "Reschedule request rejected. The candidate’s current interview remains unchanged."
        + (f" Reason: {reason}" if reason else "")
    )
    logger.info("[rejectRescheduleRequest] requestId=%s", request_id)

    return RejectRescheduleRequestResponse(
        requestId=request_id,
        oldScheduledInterviewId=scheduled_interview_id,
        candidateId=(request_doc.get("candidateId") or "").strip(),
        recruiterEmail=(request_doc.get("recruiterEmail") or "").strip().lower(),
        jobId=(request_doc.get("jobId") or "").strip(),
        jobTitle=request_doc.get("jobTitle"),
        rejected=True,
        messageText=message_text,
        message="Reschedule request rejected successfully.",
    )


async def list_reschedule_requests_logic(*, db, coll_reschedule_requests: str, recruiter_email: Optional[str] = None):
    query = {}
    if recruiter_email:
        query["recruiterEmail"] = recruiter_email.strip().lower()
    docs = await db[coll_reschedule_requests].find(query).sort("createdAt", -1).to_list(length=200)
    items = [
        RescheduleRequestDashboardItem(
            requestId=d.get("requestId"),
            scheduledInterviewId=d.get("scheduledInterviewId"),
            candidateId=d.get("candidateId"),
            candidateName=d.get("candidateName"),
            recruiterEmail=d.get("recruiterEmail"),
            jobId=d.get("jobId"),
            jobTitle=d.get("jobTitle"),
            requestStatus=d.get("requestStatus"),
            requestedSlotId=d.get("requestedSlotId"),
            requestedSlotDisplayText=d.get("requestedSlotDisplayText"),
            requestedAt=d.get("requestedAt").isoformat() if d.get("requestedAt") else None,
            reviewedAt=d.get("reviewedAt").isoformat() if d.get("reviewedAt") else None,
            reviewedBy=d.get("reviewedBy"),
            reviewComment=d.get("reviewComment"),
        )
        for d in docs
    ]
    return ListRescheduleRequestsResponse(items=items, message="Reschedule requests fetched successfully.")


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
    coll_reschedule_requests: str,
    utcnow_fn: Callable[[], Any],
    logger,
) -> RescheduleCandidateInterviewResponse:
    # Backward-compatible wrapper: this endpoint no longer executes a direct reschedule.
    start_response = await start_candidate_reschedule_request_logic(
        payload=StartCandidateRescheduleRequest(
            scheduledInterviewId=payload.scheduledInterviewId,
            requestedBy=payload.requestedBy,
        ),
        db=db,
        coll_scheduled_interviews=coll_scheduled_interviews,
        coll_candidate_sessions=coll_candidate_sessions,
        coll_recruiters=coll_recruiters,
        coll_avail_slots=coll_avail_slots,
        coll_reschedule_requests=coll_reschedule_requests,
        utcnow_fn=utcnow_fn,
        logger=logger,
    )
    return RescheduleCandidateInterviewResponse(
        oldScheduledInterviewId=start_response.oldScheduledInterviewId,
        newSessionId=start_response.sessionId,
        candidateId=start_response.candidateId,
        recruiterEmail=start_response.recruiterEmail,
        jobId=start_response.jobId,
        jobTitle=start_response.jobTitle,
        oldSlotReopened=False,
        cancelledReminderCount=0,
        slots=start_response.slots,
        messageText=start_response.messageText,
        hasMore=start_response.hasMore,
        nextAction=start_response.nextAction,
        availableActions=start_response.availableActions,
        message="Direct reschedule is deprecated. Reschedule approval flow started instead.",
    )
