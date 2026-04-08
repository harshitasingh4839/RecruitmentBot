import logging
import os
import uvicorn
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Optional, Any

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from db import get_db
from agent import (
    run_candidate_scheduler_agent,
    resolve_candidate_scheduling_session_tool,
    get_next_available_slots_tool,
    confirm_candidate_slot_booking_tool,
    cancel_candidate_interview_tool,
    start_candidate_reschedule_request_tool,
    create_candidate_reschedule_request_tool,
)
from schemas import (
    RunCandidateWhatsappAgentRequest,
    RunCandidateWhatsappAgentResponse,
)

logger = logging.getLogger(__name__)

COLL_CANDIDATE_SESSIONS = "candidateSchedulingSessions"
COLL_SCHEDULED_INTERVIEWS = "scheduledInterviews"


def utcnow():
    return datetime.now(timezone.utc)


# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     db = get_db()

#     # Helpful indexes for this server's lookups
#     await db[COLL_CANDIDATE_SESSIONS].create_index(
#         [("candidateId", 1), ("recruiterEmail", 1), ("jobId", 1), ("status", 1), ("updatedAt", -1)],
#         name="candidate_whatsapp_agent_lookup"
#     )
#     yield


app = FastAPI(
    title="Candidate WhatsApp Agent Server",
    version="1.0",
    # lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def validate_recruiter_job_metadata(job_id: str, job_title: Optional[str]) -> tuple[str, Optional[str]]:
    clean_job_id = job_id.strip()
    if not clean_job_id:
        raise HTTPException(status_code=400, detail="jobId is required.")
    clean_job_title = job_title.strip() if job_title else None
    return clean_job_id, (clean_job_title or None)


async def _load_candidate_session_context(
    *,
    db,
    recruiter_email: str,
    candidate_id: str,
    job_id: str,
    requested_session_id: Optional[str],
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    base_query = {
        "candidateId": candidate_id,
        "recruiterEmail": recruiter_email,
        "jobId": job_id,
    }

    session_doc = None
    scheduled_interview_doc = await db[COLL_SCHEDULED_INTERVIEWS].find_one(
        {
            "candidateId": candidate_id,
            "recruiterEmail": recruiter_email,
            "jobId": job_id,
            "status": {"$in": ["scheduled", "reschedule_requested"]},
        },
        sort=[("updatedAt", -1)],
        projection={"_id": 0}
    )

    # Honor an explicitly supplied session only when it belongs to the same candidate/recruiter/job.
    if requested_session_id:
        session_doc = await db[COLL_CANDIDATE_SESSIONS].find_one(
            {
                "sessionId": requested_session_id,
                **base_query,
            },
            projection={"_id": 0}
        )

    # Prefer the latest active session when the caller does not provide a sessionId.
    if not session_doc:
        session_doc = await db[COLL_CANDIDATE_SESSIONS].find_one(
            {
                **base_query,
                "status": "active",
            },
            sort=[("updatedAt", -1)],
            projection={"_id": 0}
        )

    # If there is an active/current scheduled interview, prefer its session over an arbitrary
    # latest historical session so follow-up actions like cancel target the live interview.
    if not session_doc and scheduled_interview_doc and scheduled_interview_doc.get("sessionId"):
        session_doc = await db[COLL_CANDIDATE_SESSIONS].find_one(
            {
                "sessionId": scheduled_interview_doc["sessionId"],
                **base_query,
            },
            projection={"_id": 0}
        )

    # If there is still no matching session, recover the latest session so we can keep
    # WhatsApp context attached to the same candidate/recruiter/job thread.
    if not session_doc:
        session_doc = await db[COLL_CANDIDATE_SESSIONS].find_one(
            base_query,
            sort=[("updatedAt", -1)],
            projection={"_id": 0}
        )

    return session_doc, scheduled_interview_doc


async def _persist_whatsapp_session_state(
    *,
    db,
    session_id: Optional[str],
    history: list[dict[str, str]],
    context: dict[str, Any],
    now,
) -> None:
    if not session_id:
        return

    await db[COLL_CANDIDATE_SESSIONS].update_one(
        {"sessionId": session_id},
        {
            "$set": {
                "conversationHistory": history,
                "lastAgentContext": context,
                "channel": "whatsapp",
                "updatedAt": now,
            }
        }
    )


def _normalize_user_message(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _is_numeric_slot_selection(text: str) -> bool:
    return text in {"1", "2", "3"}


def _is_more_slots_message(text: str) -> bool:
    if text in {
        "more slots",
        "show more slots",
        "show more",
        "more",
        "next slots",
        "another slot",
        "another slots",
    }:
        return True
    return "more slots" in text or "another slot" in text or "next slots" in text


def _is_cancel_message(text: str) -> bool:
    if text in {
        "cancel",
        "cancel interview",
        "cancel my interview",
        "delete interview",
        "remove interview",
    }:
        return True
    return "cancel" in text and "interview" in text


def _is_reschedule_message(text: str) -> bool:
    return text in {
        "reschedule",
        "reschedule interview",
        "reschedule my interview",
        "change interview",
        "change my interview",
    }


def _is_yes_message(text: str) -> bool:
    return text in {"yes", "yes reschedule", "yes, reschedule", "y", "yeah", "yes please"}


def _last_assistant_prompted_reschedule_confirmation(history: list[dict[str, str]]) -> bool:
    for item in reversed(history):
        if item.get("role") != "assistant":
            continue
        content = (item.get("content") or "").lower()
        return "recruiter approval" in content and "yes" in content and "no" in content
    return False


def _tool_error_detail(result: dict[str, Any]) -> str:
    error = result.get("error")
    if isinstance(error, dict):
        detail = error.get("detail")
        if isinstance(detail, str):
            return detail
    return "The requested action could not be completed right now."


async def _handle_deterministic_candidate_turn(
    *,
    user_message: str,
    history: list[dict[str, str]],
    context: dict[str, Any],
) -> Optional[tuple[str, list[dict[str, str]], dict[str, Any]]]:
    normalized = _normalize_user_message(user_message)

    if _is_numeric_slot_selection(normalized):
        selected_index = int(normalized)
        if context.get("flowType") == "reschedule_request" and context.get("activeSessionId"):
            result = await create_candidate_reschedule_request_tool(
                {"selectedIndex": selected_index},
                context,
            )
            if result.get("ok") is False:
                detail = _tool_error_detail(result)
                if "no longer active" in detail.lower():
                    reply_text = (
                        f"Sorry, I couldn't submit option {selected_index} because the reschedule session is no longer active.\n\n"
                        'Please reply "reschedule" once more and I will restart it with fresh alternate slots.'
                    )
                else:
                    reply_text = f"Sorry, I couldn't submit that reschedule request right now. {detail}"
            else:
                reply_text = result.get("messageText") or "Your reschedule request has been submitted."

            updated_history = list(history)
            updated_history.append({"role": "user", "content": user_message})
            updated_history.append({"role": "assistant", "content": reply_text})
            return reply_text, updated_history, context

        if context.get("activeSessionId"):
            result = await confirm_candidate_slot_booking_tool(
                {"selectedIndex": selected_index},
                context,
            )
            if result.get("ok") is False:
                detail = _tool_error_detail(result)
                reply_text = f"Sorry, I couldn't confirm that slot right now. {detail}"
            else:
                reply_text = result.get("messageText") or "Your interview has been confirmed."

            updated_history = list(history)
            updated_history.append({"role": "user", "content": user_message})
            updated_history.append({"role": "assistant", "content": reply_text})
            return reply_text, updated_history, context

    if _is_more_slots_message(normalized) and context.get("activeSessionId"):
        result = await get_next_available_slots_tool({}, context)
        if result.get("ok") is False:
            detail = _tool_error_detail(result)
            reply_text = f"Sorry, I couldn't fetch more slots right now. {detail}"
        else:
            reply_text = result.get("messageText") or "Here are a few more slots."

        updated_history = list(history)
        updated_history.append({"role": "user", "content": user_message})
        updated_history.append({"role": "assistant", "content": reply_text})
        return reply_text, updated_history, context

    if _is_cancel_message(normalized) and context.get("scheduledInterviewId"):
        result = await cancel_candidate_interview_tool({}, context)
        if result.get("ok") is False:
            detail = _tool_error_detail(result)
            reply_text = f"Sorry, I couldn't cancel the interview right now. {detail}"
        else:
            reply_text = result.get("messageText") or "Your interview has been cancelled."

        updated_history = list(history)
        updated_history.append({"role": "user", "content": user_message})
        updated_history.append({"role": "assistant", "content": reply_text})
        return reply_text, updated_history, context

    if (
        _is_yes_message(normalized)
        and context.get("scheduledInterviewId")
        and not context.get("activeSessionId")
        and _last_assistant_prompted_reschedule_confirmation(history)
    ):
        result = await start_candidate_reschedule_request_tool({}, context)
        if result.get("ok") is False:
            detail = _tool_error_detail(result)
            reply_text = f"Sorry, I couldn't start the reschedule request right now. {detail}"
        else:
            reply_text = result.get("messageText") or "Here are alternate slots for your reschedule request."

        updated_history = list(history)
        updated_history.append({"role": "user", "content": user_message})
        updated_history.append({"role": "assistant", "content": reply_text})
        return reply_text, updated_history, context

    if _is_reschedule_message(normalized) and context.get("flowType") == "reschedule_request" and context.get("activeSessionId"):
        reply_text = (
            "Your reschedule request is already in progress.\n\n"
            'Reply with 1, 2, or 3 to pick one of the shown alternate slots, or reply "more slots".'
        )
        updated_history = list(history)
        updated_history.append({"role": "user", "content": user_message})
        updated_history.append({"role": "assistant", "content": reply_text})
        return reply_text, updated_history, context

    return None


@app.post(
    "/agent/runCandidateWhatsappAgent",
    response_model=RunCandidateWhatsappAgentResponse
)
async def run_candidate_whatsapp_agent(
    payload: RunCandidateWhatsappAgentRequest,
    db=Depends(get_db)
):
    recruiter_email = payload.recruiterEmail.lower().strip()
    candidate_id = payload.candidateId.strip()
    job_id, clean_job_title = validate_recruiter_job_metadata(payload.jobId, payload.jobTitle)
    now = utcnow()
    session_doc, scheduled_interview_doc = await _load_candidate_session_context(
        db=db,
        recruiter_email=recruiter_email,
        candidate_id=candidate_id,
        job_id=job_id,
        requested_session_id=payload.sessionId,
    )
    resolved_session_id = (session_doc or {}).get("sessionId")

    history = []
    saved_context = {}

    if session_doc:
        history = session_doc.get("conversationHistory") or []
        saved_context = session_doc.get("lastAgentContext") or {}

    context = {
        "candidateId": candidate_id,
        "recruiterEmail": recruiter_email,
        "jobId": job_id,
        "jobTitle": clean_job_title or saved_context.get("jobTitle"),
        "provider": payload.provider or saved_context.get("provider") or "google",
        "timezone": payload.timezone or saved_context.get("timezone") or "Asia/Kolkata",
        "mode": payload.mode or saved_context.get("mode") or "google_meet",
        "activeSessionId": (
            (session_doc or {}).get("sessionId")
            if (session_doc or {}).get("status") == "active"
            else None
        ),
        "scheduledInterviewId": (
            (
                (session_doc or {}).get("scheduledInterviewId")
                if (session_doc or {}).get("status") == "active"
                else None
            )
            or (scheduled_interview_doc or {}).get("scheduledInterviewId")
            or (session_doc or {}).get("scheduledInterviewId")
            or saved_context.get("scheduledInterviewId")
        ),
        "flowType": (
            (session_doc or {}).get("flowType")
            if (session_doc or {}).get("status") == "active"
            else None
        ),
        "rescheduleRequestState": (
            (session_doc or {}).get("rescheduleRequestState")
            if (session_doc or {}).get("status") == "active"
            else None
        ),
        "pendingRescheduleRequestId": (
            (session_doc or {}).get("pendingRescheduleRequestId")
            if (session_doc or {}).get("status") == "active"
            else None
        ),
    }

    # recruiter-triggered outbound start
    if payload.triggerType == "outbound_start":
        user_message = payload.userPrompt or "start"
    else:
        user_message = (payload.userPrompt or "").strip()
        if not user_message:
            raise HTTPException(status_code=400, detail="userPrompt is required for candidate_reply.")

    # If no history and no active session yet, prime the conversation with opening message
    # This matches your test.py style first-step flow
    if not history and not context.get("activeSessionId") and payload.triggerType == "outbound_start":
        initial = await resolve_candidate_scheduling_session_tool({}, context)

        base_intro = (
            f"Hi! You’ve been shortlisted for the {context.get('jobTitle') or 'role'} role "
            f"(Job ID: {context['jobId']})."
        )

        message_text = initial.get("messageText") or "I’m here to help you with your interview scheduling."
        next_action = initial.get("nextAction")

        if next_action in {"new_session_created", "continue_session"}:
            full_opening = (
                f"{base_intro} "
                "Please schedule your interview by choosing one of the available slots below.\n\n"
                f"{message_text}"
            )
        elif next_action == "already_scheduled":
            full_opening = f"{base_intro} {message_text}"
        else:
            full_opening = f"{base_intro}\n\n{message_text}"

        history.append({"role": "assistant", "content": full_opening})

        session_id_to_persist = context.get("activeSessionId") or resolved_session_id
        await _persist_whatsapp_session_state(
            db=db,
            session_id=session_id_to_persist,
            history=history,
            context=context,
            now=now,
        )

        return RunCandidateWhatsappAgentResponse(
            replyText=full_opening,
            candidateId=candidate_id,
            recruiterEmail=recruiter_email,
            jobId=job_id,
            jobTitle=context.get("jobTitle"),
            sessionId=session_id_to_persist,
            scheduledInterviewId=context.get("scheduledInterviewId"),
            availableActions=initial.get("availableActions", []),
            nextAction=initial.get("nextAction"),
            message="Candidate WhatsApp agent response generated successfully.",
        )

    deterministic_turn = await _handle_deterministic_candidate_turn(
        user_message=user_message,
        history=history,
        context=context,
    )
    if deterministic_turn:
        reply_text, updated_history, updated_context = deterministic_turn
        session_id_to_persist = (
            updated_context.get("persistSessionId")
            or updated_context.get("activeSessionId")
            or resolved_session_id
        )
        await _persist_whatsapp_session_state(
            db=db,
            session_id=session_id_to_persist,
            history=updated_history,
            context=updated_context,
            now=now,
        )

        return RunCandidateWhatsappAgentResponse(
            replyText=reply_text,
            candidateId=candidate_id,
            recruiterEmail=recruiter_email,
            jobId=job_id,
            jobTitle=updated_context.get("jobTitle"),
            sessionId=session_id_to_persist,
            scheduledInterviewId=updated_context.get("scheduledInterviewId"),
            availableActions=[],
            nextAction=None,
            message="Candidate WhatsApp agent response generated successfully.",
        )

    # Regular conversational turn
    reply_text, updated_history, updated_context = await run_candidate_scheduler_agent(
        user_message=user_message,
        conversation_history=history,
        context=context,
    )

    session_id_to_persist = (
        updated_context.get("persistSessionId")
        or updated_context.get("activeSessionId")
        or resolved_session_id
    )
    await _persist_whatsapp_session_state(
        db=db,
        session_id=session_id_to_persist,
        history=updated_history,
        context=updated_context,
        now=now,
    )

    return RunCandidateWhatsappAgentResponse(
        replyText=reply_text,
        candidateId=candidate_id,
        recruiterEmail=recruiter_email,
        jobId=job_id,
        jobTitle=updated_context.get("jobTitle"),
        sessionId=session_id_to_persist,
        scheduledInterviewId=updated_context.get("scheduledInterviewId"),
        availableActions=[],
        nextAction=None,
        message="Candidate WhatsApp agent response generated successfully.",
    )
