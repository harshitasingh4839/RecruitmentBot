import logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Optional, Any

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from db import get_db
from agent import run_candidate_scheduler_agent, resolve_candidate_scheduling_session_tool
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

    session_doc = None

    # 1) Load current active session if sessionId is provided
    if payload.sessionId:
        session_doc = await db[COLL_CANDIDATE_SESSIONS].find_one(
            {"sessionId": payload.sessionId},
            projection={"_id": 0}
        )

    # 2) Else fallback to latest active session for candidate+recruiter+job
    if not session_doc:
        session_doc = await db[COLL_CANDIDATE_SESSIONS].find_one(
            {
                "candidateId": candidate_id,
                "recruiterEmail": recruiter_email,
                "jobId": job_id,
                "status": "active",
            },
            sort=[("updatedAt", -1)],
            projection={"_id": 0}
        )

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
        "activeSessionId": (session_doc or {}).get("sessionId") or saved_context.get("activeSessionId"),
        "scheduledInterviewId": (session_doc or {}).get("scheduledInterviewId") or saved_context.get("scheduledInterviewId"),
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

        session_id = context.get("activeSessionId")
        if session_id:
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

        return RunCandidateWhatsappAgentResponse(
            replyText=full_opening,
            candidateId=candidate_id,
            recruiterEmail=recruiter_email,
            jobId=job_id,
            jobTitle=context.get("jobTitle"),
            sessionId=context.get("activeSessionId"),
            scheduledInterviewId=context.get("scheduledInterviewId"),
            availableActions=initial.get("availableActions", []),
            nextAction=initial.get("nextAction"),
            message="Candidate WhatsApp agent response generated successfully.",
        )

    # Regular conversational turn
    reply_text, updated_history, updated_context = await run_candidate_scheduler_agent(
        user_message=user_message,
        conversation_history=history,
        context=context,
    )

    session_id = updated_context.get("activeSessionId")

    if session_id:
        await db[COLL_CANDIDATE_SESSIONS].update_one(
            {"sessionId": session_id},
            {
                "$set": {
                    "conversationHistory": updated_history,
                    "lastAgentContext": updated_context,
                    "channel": "whatsapp",
                    "updatedAt": now,
                }
            }
        )

    return RunCandidateWhatsappAgentResponse(
        replyText=reply_text,
        candidateId=candidate_id,
        recruiterEmail=recruiter_email,
        jobId=job_id,
        jobTitle=updated_context.get("jobTitle"),
        sessionId=updated_context.get("activeSessionId"),
        scheduledInterviewId=updated_context.get("scheduledInterviewId"),
        availableActions=[],
        nextAction=None,
        message="Candidate WhatsApp agent response generated successfully.",
    )