from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

from bson import ObjectId
from fastapi import HTTPException

from schemas import (
    CandidateSlotOption,
    GetNextAvailableSlotsRequest,
    GetNextAvailableSlotsResponse,
)

SESSION_TTL_HOURS = 24


def build_slot_display(slot: dict[str, Any], tz_name: str) -> str:
    start_local = slot.get("startAtLocal") or slot.get("startAtUtc")
    end_local = slot.get("endAtLocal") or slot.get("endAtUtc")
    return f"{start_local} to {end_local} ({tz_name})"


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

def format_next_slots_message(candidate_name: str, job_title: str | None, slot_options: list[CandidateSlotOption]) -> str:
    first_name = candidate_name.split()[0] if candidate_name else "there"

    if not slot_options:
        if job_title:
            return (
                f"Hi {first_name}, I checked for more options for the {job_title} role, "
                f"but there are no more available slots at the moment.\n\n"
                f"You can reply here later to check again."
            )
        return (
            f"Hi {first_name}, I checked for more options, "
            f"but there are no more available slots at the moment.\n\n"
            f"You can reply here later to check again."
        )

    lines = [f"Hi {first_name}, here are a few more available slots:", ""]
    for idx, slot in enumerate(slot_options, start=1):
        lines.append(f"{idx}. {slot.displayText}")
    lines.append("")
    lines.append("You can reply with 1, 2, or 3 to choose a slot.")
    lines.append('If these still don’t work, reply with "more slots".')
    return "\n".join(lines)


async def _release_expired_holds(*, db, coll_avail_slots: str, now) -> int:
    result = await db[coll_avail_slots].update_many(
        {
            "status": "held",
            "holdExpiresAt": {"$lte": now},
        },
        {
            "$set": {
                "status": "active",
                "updatedAt": now,
                "holdCandidateId": None,
                "holdSessionId": None,
                "holdExpiresAt": None,
            }
        },
    )
    return result.modified_count

def _slot_options_from_raw(raw_slots: list[dict[str, Any]], timezone_name: str) -> tuple[list[CandidateSlotOption], list[str]]:
    slot_options: list[CandidateSlotOption] = []
    slot_ids: list[str] = []

    for slot in raw_slots:
        slot_id = str(slot["_id"])
        slot_ids.append(slot_id)
        slot_options.append(
            CandidateSlotOption(
                slotId=slot_id,
                displayText=build_slot_display(slot, slot.get("timezone") or timezone_name),
                startAtUtc=slot.get("startAtUtc"),
                endAtUtc=slot.get("endAtUtc"),
                startAtLocal=slot.get("startAtLocal"),
                endAtLocal=slot.get("endAtLocal"),
            )
        )

    return slot_options, slot_ids


async def get_next_available_slots_logic(
    *,
    payload: GetNextAvailableSlotsRequest,
    db,
    coll_avail_slots: str,
    coll_candidate_sessions: str,
    utcnow_fn: Callable[[], Any],
    logger,
) -> GetNextAvailableSlotsResponse:
    session_id = payload.sessionId.strip()
    logger.info("[getNextAvailableSlots] sessionId=%s", session_id)

    session = await db[coll_candidate_sessions].find_one({"sessionId": session_id})
    if not session:
        raise HTTPException(status_code=404, detail="Scheduling session not found.")
    if session.get("status") != "active":
        raise HTTPException(status_code=400, detail="This scheduling session is no longer active.")

    recruiter_email = (session.get("recruiterEmail") or "").strip().lower()
    candidate_id = (session.get("candidateId") or "").strip()
    candidate_name = (session.get("candidateName") or "").strip()
    job_id = (session.get("jobId") or "").strip()
    job_title = session.get("jobTitle")
    timezone = session.get("timezone") or "Asia/Kolkata"
    last_shown_start_at_utc = session.get("lastShownStartAtUtc")
    shown_slot_ids = session.get("shownSlotIds") or []
    seen_slot_ids = session.get("seenSlotIds") or shown_slot_ids

    now = utcnow_fn()
    released_holds = await _release_expired_holds(
        db=db,
        coll_avail_slots=coll_avail_slots,
        now=now,
    )
    if released_holds:
        logger.info("[getNextAvailableSlots] released_expired_holds=%s sessionId=%s", released_holds, session_id)

    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    start_at_filter = {"$gt": last_shown_start_at_utc} if last_shown_start_at_utc and last_shown_start_at_utc > now_iso else {"$gt": now_iso}

    query = {
        "recruiterEmail": recruiter_email,
        "jobId": job_id,
        "status": "active",
        "startAtUtc": start_at_filter,
    }

    exclude_object_ids = _safe_object_ids(seen_slot_ids)
    if exclude_object_ids:
        query["_id"] = {"$nin": exclude_object_ids}

    raw_slots = await db[coll_avail_slots].find(query).sort("startAtUtc", 1).limit(3).to_list(length=3)

    slot_options, new_slot_ids = _slot_options_from_raw(raw_slots, timezone)

    update_doc = {"updatedAt": now, "expiresAt": _session_expiry(now)}
    if slot_options:
        update_doc["shownSlotIds"] = new_slot_ids
        update_doc["lastShownStartAtUtc"] = slot_options[-1].startAtUtc

    session_update: dict[str, Any] = {"$set": update_doc}
    if new_slot_ids:
        session_update["$addToSet"] = {"seenSlotIds": {"$each": new_slot_ids}}

    await db[coll_candidate_sessions].update_one(
        {"sessionId": session_id},
        session_update,
    )

    exhausted = not slot_options
    has_more = len(slot_options) == 3

    logger.info(
        "[getNextAvailableSlots] sessionId=%s candidateId=%s slots=%d",
        session_id,
        candidate_id,
        len(slot_options),
    )

    return GetNextAvailableSlotsResponse(
        sessionId=session_id,
        candidateId=candidate_id,
        recruiterEmail=recruiter_email,
        jobId=job_id,
        jobTitle=job_title,
        slots=slot_options,
        messageText=format_next_slots_message(candidate_name, job_title, slot_options),
        hasMore=has_more,
        nextAction="no_more_slots" if exhausted else "show_slots",
        availableActions=["check_later"] if exhausted else ["select_slot", "more_slots"],
        message="No more slots available." if exhausted else "Next available slots fetched successfully.",
    )
