from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any, Callable, Optional

from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from candidate_scheduling import build_slot_display
from schemas import (
    CandidateSlotOption,
    ResolveCandidateSchedulingSessionRequest,
    ResolveCandidateSchedulingSessionResponse,
)

SESSION_TTL_HOURS = 24

def _session_expiry(now):
    return now + timedelta(hours=SESSION_TTL_HOURS)


def _safe_object_ids(values: list[str]) -> list[ObjectId]:
    out: list[ObjectId] = []
    for value in values:
        try:
            out.append(ObjectId(value))
        except Exception:
            continue
    return out


async def _get_recruiter_doc(db, coll_recruiters: str, recruiter_email: str) -> dict[str, Any]:
    recruiter = await db[coll_recruiters].find_one({"email": recruiter_email})
    if not recruiter:
        raise HTTPException(status_code=404, detail="Recruiter not found in recruiterData.")

    recruiter_name = (recruiter.get("name") or "").strip()
    recruiter_phone = (recruiter.get("phone") or "").strip()
    recruiter_id = (recruiter.get("recruiterId") or "").strip()
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


def _format_slot_message(
    candidate_name: str,
    job_title: Optional[str],
    slot_options: list[CandidateSlotOption]
) -> str:
    intro = f"Hi {candidate_name},"
    if job_title:
        intro += f" here are the next available interview slots for the {job_title} role:"
    else:
        intro += " here are the next available interview slots:"

    lines = [intro, ""]
    for idx, slot in enumerate(slot_options, start=1):
        lines.append(f"{idx}. {slot.displayText}")
    lines.append("")
    lines.append("Reply with 1, 2, or 3 to choose a slot, or say 'more slots'.")
    return "\n".join(lines)


def _build_slot_options(
    raw_slots: list[dict[str, Any]],
    fallback_timezone: str,
) -> tuple[list[CandidateSlotOption], list[str]]:
    slot_options: list[CandidateSlotOption] = []
    shown_slot_ids: list[str] = []

    for slot in raw_slots:
        slot_id = str(slot["_id"])
        shown_slot_ids.append(slot_id)
        slot_options.append(
            CandidateSlotOption(
                slotId=slot_id,
                displayText=build_slot_display(slot, slot.get("timezone") or fallback_timezone),
                startAtUtc=slot.get("startAtUtc"),
                endAtUtc=slot.get("endAtUtc"),
                startAtLocal=slot.get("startAtLocal"),
                endAtLocal=slot.get("endAtLocal"),
            )
        )

    return slot_options, shown_slot_ids


async def _fetch_first_available_slots(
    *,
    db,
    coll_avail_slots: str,
    recruiter_email: str,
    now_iso: str,
    timezone: str,
) -> tuple[list[CandidateSlotOption], list[str]]:
    raw_slots = await db[coll_avail_slots].find(
        {
            "recruiterEmail": recruiter_email,
            "status": "active",
            "startAtUtc": {"$gt": now_iso},
        }
    ).sort("startAtUtc", 1).limit(3).to_list(length=3)

    return _build_slot_options(raw_slots, timezone)


async def resolve_candidate_scheduling_session_logic(
    *,
    payload: ResolveCandidateSchedulingSessionRequest,
    db,
    coll_candidates: str,
    coll_recruiters: str,
    coll_candidate_sessions: str,
    coll_avail_slots: str,
    coll_scheduled_interviews: str,
    validate_recruiter_job_metadata: Callable[[str, Optional[str]], tuple[str, Optional[str]]],
    utcnow_fn: Callable[[], Any],
    logger,
) -> ResolveCandidateSchedulingSessionResponse:
    recruiter_email = payload.recruiterEmail.lower().strip()
    candidate_id = payload.candidateId.strip()
    job_id, clean_job_title = validate_recruiter_job_metadata(payload.jobId, payload.jobTitle)
    now = utcnow_fn()
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info(
        "[resolveCandidateSchedulingSession] recruiter=%s candidateId=%s jobId=%s",
        recruiter_email,
        candidate_id,
        job_id,
    )

    recruiter = await _get_recruiter_doc(db, coll_recruiters, recruiter_email)

    # 1) Check latest active session
    session = await db[coll_candidate_sessions].find_one(
        {
            "candidateId": candidate_id,
            "recruiterEmail": recruiter_email,
            "jobId": job_id,
            "status": "active",
        },
        sort=[("updatedAt", -1)],
    )

    if session:
        session_id = session["sessionId"]
        candidate_name = (session.get("candidateName") or "").strip() or "there"
        session_job_title = session.get("jobTitle") or clean_job_title
        session_timezone = session.get("timezone") or payload.timezone or "Asia/Kolkata"
        scheduled_interview_id = session.get("scheduledInterviewId")

        # 1a) If session already has a scheduled interview
        if scheduled_interview_id:
            scheduled_doc = await db[coll_scheduled_interviews].find_one(
                {"scheduledInterviewId": scheduled_interview_id},
                projection={"_id": 0, "status": 1},
            )
            if scheduled_doc and scheduled_doc.get("status") in {"scheduled", "rescheduled", "reschedule_requested"}:
                await db[coll_candidate_sessions].update_one(
                    {"sessionId": session_id},
                    {"$set": {"updatedAt": now, "expiresAt": _session_expiry(now)}},
                )
                return ResolveCandidateSchedulingSessionResponse(
                    nextAction="already_scheduled",
                    sessionId=session_id,
                    scheduledInterviewId=scheduled_interview_id,
                    candidateId=candidate_id,
                    recruiterEmail=recruiter_email,
                    jobId=job_id,
                    jobTitle=session_job_title,
                    slots=[],
                    hasMore=False,
                    availableActions=["reschedule", "cancel"],
                    messageText=(
                        f"Hi {candidate_name}, your interview is already scheduled. "
                        f"If you’d like to reschedule, I can initiate a request for recruiter approval, or I can help you cancel it."
                    ),
                    message="Active scheduled interview already exists for this session.",
                )

        # 1b) Try to reuse already shown valid slots
        shown_slot_ids = session.get("shownSlotIds") or []
        slot_object_ids = _safe_object_ids(shown_slot_ids)

        if slot_object_ids:
            raw_slots = await db[coll_avail_slots].find(
                {
                    "_id": {"$in": slot_object_ids},
                    "status": "active",
                    "startAtUtc": {"$gt": now_iso},
                }
            ).sort("startAtUtc", 1).to_list(length=3)

            slot_options, refreshed_slot_ids = _build_slot_options(raw_slots, session_timezone)

            if slot_options:
                await db[coll_candidate_sessions].update_one(
                    {"sessionId": session_id},
                    {
                        "$set": {
                            "shownSlotIds": refreshed_slot_ids,
                            "lastShownStartAtUtc": slot_options[-1].startAtUtc,
                            "updatedAt": now,
                            "expiresAt": _session_expiry(now),
                        }
                    },
                )

                return ResolveCandidateSchedulingSessionResponse(
                    nextAction="continue_session",
                    sessionId=session_id,
                    scheduledInterviewId=None,
                    candidateId=candidate_id,
                    recruiterEmail=recruiter_email,
                    jobId=job_id,
                    jobTitle=session_job_title,
                    slots=slot_options,
                    hasMore=len(slot_options) == 3,
                    availableActions=["select_slot", "more_slots"],
                    messageText=_format_slot_message(candidate_name, session_job_title, slot_options),
                    message="Existing active scheduling session found.",
                )

        # 1c) Existing session found, but previous shown slots are stale/missing.
        # Refresh the same session with current available slots.
        fresh_slot_options, fresh_slot_ids = await _fetch_first_available_slots(
            db=db,
            coll_avail_slots=coll_avail_slots,
            recruiter_email=recruiter_email,
            now_iso=now_iso,
            timezone=session_timezone,
        )

        await db[coll_candidate_sessions].update_one(
            {"sessionId": session_id},
            {
                "$set": {
                    "shownSlotIds": fresh_slot_ids,
                    "lastShownStartAtUtc": fresh_slot_options[-1].startAtUtc if fresh_slot_options else None,
                    "updatedAt": now,
                    "expiresAt": _session_expiry(now),
                }
            },
        )

        if fresh_slot_options:
            return ResolveCandidateSchedulingSessionResponse(
                nextAction="continue_session",
                sessionId=session_id,
                scheduledInterviewId=None,
                candidateId=candidate_id,
                recruiterEmail=recruiter_email,
                jobId=job_id,
                jobTitle=session_job_title,
                slots=fresh_slot_options,
                hasMore=len(fresh_slot_options) == 3,
                availableActions=["select_slot", "more_slots"],
                messageText=_format_slot_message(candidate_name, session_job_title, fresh_slot_options),
                message="Existing active scheduling session found and refreshed with current slots.",
            )

        return ResolveCandidateSchedulingSessionResponse(
            nextAction="no_slots_available",
            sessionId=session_id,
            scheduledInterviewId=None,
            candidateId=candidate_id,
            recruiterEmail=recruiter_email,
            jobId=job_id,
            jobTitle=session_job_title,
            slots=[],
            hasMore=False,
            availableActions=["check_later"],
            messageText=(
                f"Hi {candidate_name}, there are no available interview slots at the moment. "
                f"Please check again later."
            ),
            message="Active session exists, but no active slots are currently available.",
        )

    # 2) No active session found. Check if already scheduled elsewhere.
    latest_scheduled = await db[coll_scheduled_interviews].find_one(
        {
            "candidateId": candidate_id,
            "recruiterEmail": recruiter_email,
            "jobId": job_id,
            "status": {"$in": ["scheduled", "rescheduled", "reschedule_requested"]},
        },
        sort=[("updatedAt", -1)],
    )

    if latest_scheduled:
        candidate_name = (latest_scheduled.get("candidateName") or "").strip() or "there"
        return ResolveCandidateSchedulingSessionResponse(
            nextAction="already_scheduled",
            sessionId=latest_scheduled.get("sessionId"),
            scheduledInterviewId=latest_scheduled.get("scheduledInterviewId"),
            candidateId=candidate_id,
            recruiterEmail=recruiter_email,
            jobId=job_id,
            jobTitle=latest_scheduled.get("jobTitle") or clean_job_title,
            slots=[],
            hasMore=False,
            availableActions=["reschedule", "cancel"],
            messageText=(
                f"Hi {candidate_name}, your interview is already scheduled. "
                f"If you’d like to reschedule, I can initiate a request for recruiter approval, or I can help you cancel it."
            ),
            message="Latest scheduled interview already exists for this candidate and recruiter.",
        )

    # 3) No session -> validate candidate
    candidate = await db[coll_candidates].find_one({"candidateId": candidate_id})
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found in candidateData.")

    candidate_name = (candidate.get("name") or "").strip()
    candidate_phone = (candidate.get("phone") or "").strip()
    candidate_email = (candidate.get("email") or "").strip() or None

    if not candidate_name:
        raise HTTPException(status_code=400, detail="Candidate name missing in candidateData.")
    if not candidate_phone:
        raise HTTPException(status_code=400, detail="Candidate phone missing in candidateData.")
    if not candidate_email:
        raise HTTPException(status_code=400, detail="Candidate email missing in candidateData.")

    slot_options, shown_slot_ids = await _fetch_first_available_slots(
        db=db,
        coll_avail_slots=coll_avail_slots,
        recruiter_email=recruiter_email,
        now_iso=now_iso,
        timezone=payload.timezone,
    )

    if not slot_options:
        return ResolveCandidateSchedulingSessionResponse(
            nextAction="no_slots_available",
            sessionId=None,
            scheduledInterviewId=None,
            candidateId=candidate_id,
            recruiterEmail=recruiter_email,
            jobId=job_id,
            jobTitle=clean_job_title,
            slots=[],
            hasMore=False,
            availableActions=["check_later"],
            messageText=(
                f"Hi {candidate_name}, there are no available interview slots at the moment. "
                f"Please check again later."
            ),
            message="No active slots available.",
        )

    session_id = "sess_" + secrets.token_urlsafe(16)
    session_doc = {
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
        "jobTitle": clean_job_title,
        "provider": payload.provider,
        "timezone": payload.timezone,
        "mode": payload.mode,
        "status": "active",
        "shownSlotIds": shown_slot_ids,
        "lastShownStartAtUtc": slot_options[-1].startAtUtc,
        "scheduledInterviewId": None,
        "createdAt": now,
        "updatedAt": now,
        "expiresAt": _session_expiry(now),
    }

    try:
        await db[coll_candidate_sessions].insert_one(session_doc)
    except DuplicateKeyError:
        logger.warning(
            "[resolveCandidateSchedulingSession] duplicate active session hit candidateId=%s recruiter=%s jobId=%s",
            candidate_id,
            recruiter_email,
            job_id,
        )
        existing = await db[coll_candidate_sessions].find_one(
            {
                "candidateId": candidate_id,
                "recruiterEmail": recruiter_email,
                "jobId": job_id,
                "status": "active",
            },
            sort=[("updatedAt", -1)],
        )
        if not existing:
            raise HTTPException(status_code=409, detail="An active scheduling session already exists. Please retry.")
        session_id = existing["sessionId"]

    return ResolveCandidateSchedulingSessionResponse(
        nextAction="new_session_created",
        sessionId=session_id,
        scheduledInterviewId=None,
        candidateId=candidate_id,
        recruiterEmail=recruiter_email,
        jobId=job_id,
        jobTitle=clean_job_title,
        slots=slot_options,
        hasMore=len(slot_options) == 3,
        availableActions=["select_slot", "more_slots"],
        messageText=_format_slot_message(candidate_name, clean_job_title, slot_options),
        message="New candidate scheduling session created successfully.",
    )
