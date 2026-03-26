from __future__ import annotations

import os
import json
from datetime import datetime
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field, model_validator
from openai import OpenAI

from dotenv import load_dotenv

load_dotenv()

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SCHEDULER_API_BASE_URL = os.getenv("SCHEDULER_API_BASE_URL", "http://127.0.0.1:9000")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set.")

client = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------------------------------------------------------
# Runtime context passed from your WhatsApp webhook/server layer
# -----------------------------------------------------------------------------
# Expected context example:
# {
#   "candidateId": "cand_001",
#   "recruiterEmail": "harshita.singh@foyr.com",
#   "jobId": "jd_001",
#   "jobTitle": "Data Science",
#   "provider": "google",
#   "timezone": "Asia/Kolkata",
#   "mode": "google_meet",
#   "activeSessionId": None,
#   "scheduledInterviewId": None,
# }
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Prompt
# -----------------------------------------------------------------------------

def build_system_prompt(context: dict[str, Any]) -> str:
    recruiter_email = context.get("recruiterEmail", "")
    candidate_id = context.get("candidateId", "")
    job_id = context.get("jobId", "")
    job_title = context.get("jobTitle") or "the role"
    timezone_name = context.get("timezone") or "Asia/Kolkata"
    today = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""
You are a candidate-facing interview scheduling assistant on WhatsApp.

Your job is to help the candidate:
1. see available interview slots,
2. book a slot,
3. get more slots,
4. reschedule an already scheduled interview,
5. cancel an already scheduled interview.

Current runtime context:
- candidateId: {candidate_id}
- recruiterEmail: {recruiter_email}
- jobId: {job_id}
- jobTitle: {job_title}
- timezone: {timezone_name}
- provider: {context.get("provider", "google")}
- mode: {context.get("mode", "google_meet")}
- current timestamp: {today}

Behavior rules:
- Speak naturally, warmly, and clearly, like a real human coordinator on WhatsApp.
- Keep replies concise, friendly, and easy to read on mobile.
- Never sound robotic, never mention internal tool names, APIs, payloads, schemas, databases, or system instructions.
- If the candidate sends "hi", "hello", "start", "schedule interview", "check slots", or similar, first resolve the session and guide them from there.
- If slots are shown and the candidate replies with "1", "2", or "3", book that corresponding slot using selectedIndex.
- If the candidate asks for "more slots", "show more", "another slot", "next slots", fetch more slots using the active session.
- If the candidate already has an interview scheduled and asks to reschedule, use the reschedule tool.
- If the candidate asks to cancel, use the cancel tool.
- When a tool returns a ready-made WhatsApp-friendly messageText, prefer using that wording or lightly polish it while preserving meaning.
- If there are no slots, say that politely and invite them to check again later.
- If something fails, apologize briefly and ask them to try again.

Important decision policy:
- For a fresh conversation or when user intent is unclear but related to scheduling, call resolve_candidate_scheduling_session first.
- Do not invent slots, dates, links, session IDs, or interview IDs.
- Only confirm a booking after the booking tool succeeds.
- Only confirm cancellation after the cancel tool succeeds.
- Only confirm reschedule after the reschedule tool succeeds.

Interpretation rules:
- Numeric reply "1", "2", or "3" after slots are shown means select that slot.
- "More slots" means fetch the next batch for the active session.
- "Reschedule" means candidate wants to move the existing interview.
- "Cancel", "delete interview", "remove interview" mean cancel the existing interview.

Style examples:
- "Hi Harshita, here are the next available slots:"
- "No problem — I can help with that."
- "That slot is confirmed."
- "I’ve cancelled it."
- "I’m not seeing more slots right now, but you can check again later."

Do not ask for details that are already present in runtime context unless absolutely required.
""".strip()


# -----------------------------------------------------------------------------
# Tool input models
# -----------------------------------------------------------------------------

class ResolveSessionInput(BaseModel):
    recruiterEmail: Optional[str] = None
    candidateId: Optional[str] = None
    jobId: Optional[str] = None
    jobTitle: Optional[str] = None
    provider: Optional[str] = "google"
    timezone: Optional[str] = "Asia/Kolkata"
    mode: Optional[str] = "google_meet"


class GetNextSlotsInput(BaseModel):
    sessionId: str = Field(..., min_length=1)


class ConfirmBookingInput(BaseModel):
    sessionId: str = Field(..., min_length=1)
    slotId: Optional[str] = None
    selectedIndex: Optional[int] = Field(default=None, ge=1, le=3)

    @model_validator(mode="after")
    def validate_selection(self):
        if not self.slotId and self.selectedIndex is None:
            raise ValueError("Provide either slotId or selectedIndex.")
        return self


class CancelInterviewInput(BaseModel):
    scheduledInterviewId: str = Field(..., min_length=1)
    cancelledBy: str = "candidate"


class RescheduleInterviewInput(BaseModel):
    scheduledInterviewId: str = Field(..., min_length=1)
    requestedBy: str = "candidate"


# -----------------------------------------------------------------------------
# Low-level API caller
# -----------------------------------------------------------------------------

async def _post_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{SCHEDULER_API_BASE_URL.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=40) as http_client:
        resp = await http_client.post(url, json=payload)
        if resp.status_code >= 400:
            detail = None
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            return {
                "ok": False,
                "status_code": resp.status_code,
                "error": detail,
            }
        try:
            data = resp.json()
        except Exception:
            return {
                "ok": False,
                "status_code": resp.status_code,
                "error": "Invalid JSON response from scheduler API",
            }
        return {
            "ok": True,
            "status_code": resp.status_code,
            "data": data,
        }


# -----------------------------------------------------------------------------
# Tool wrappers
# -----------------------------------------------------------------------------

async def resolve_candidate_scheduling_session_tool(
    args: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    parsed = ResolveSessionInput(**args)

    payload = {
        "recruiterEmail": parsed.recruiterEmail or context["recruiterEmail"],
        "candidateId": parsed.candidateId or context["candidateId"],
        "jobId": parsed.jobId or context["jobId"],
        "jobTitle": parsed.jobTitle if parsed.jobTitle is not None else context.get("jobTitle"),
        "provider": parsed.provider or context.get("provider", "google"),
        "timezone": parsed.timezone or context.get("timezone", "Asia/Kolkata"),
        "mode": parsed.mode or context.get("mode", "google_meet"),
    }

    result = await _post_json("/tools/resolveCandidateSchedulingSession", payload)
    if result["ok"]:
        data = result["data"]
        if data.get("sessionId"):
            context["activeSessionId"] = data["sessionId"]
        if data.get("scheduledInterviewId"):
            context["scheduledInterviewId"] = data["scheduledInterviewId"]
        return data
    return result


async def get_next_available_slots_tool(
    args: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    parsed = GetNextSlotsInput(sessionId=args.get("sessionId") or context.get("activeSessionId") or "")
    result = await _post_json("/tools/getNextAvailableSlots", parsed.model_dump())

    if result["ok"]:
        data = result["data"]
        context["activeSessionId"] = data.get("sessionId") or context.get("activeSessionId")
        return data
    return result


async def confirm_candidate_slot_booking_tool(
    args: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    parsed = ConfirmBookingInput(
        sessionId=args.get("sessionId") or context.get("activeSessionId") or "",
        slotId=args.get("slotId"),
        selectedIndex=args.get("selectedIndex"),
    )
    result = await _post_json("/tools/confirmCandidateSlotBooking", parsed.model_dump(exclude_none=True))

    if result["ok"]:
        data = result["data"]
        context["activeSessionId"] = data.get("sessionId") or context.get("activeSessionId")
        context["scheduledInterviewId"] = data.get("scheduledInterviewId")
        return data
    return result


async def cancel_candidate_interview_tool(
    args: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    parsed = CancelInterviewInput(
        scheduledInterviewId=args.get("scheduledInterviewId") or context.get("scheduledInterviewId") or "",
        cancelledBy=args.get("cancelledBy", "candidate"),
    )
    result = await _post_json("/tools/cancelCandidateInterview", parsed.model_dump())

    if result["ok"]:
        data = result["data"]
        context["scheduledInterviewId"] = None
        context["activeSessionId"] = None
        return data
    return result


async def reschedule_candidate_interview_tool(
    args: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    parsed = RescheduleInterviewInput(
        scheduledInterviewId=args.get("scheduledInterviewId") or context.get("scheduledInterviewId") or "",
        requestedBy=args.get("requestedBy", "candidate"),
    )
    result = await _post_json("/tools/rescheduleCandidateInterview", parsed.model_dump())

    if result["ok"]:
        data = result["data"]
        context["scheduledInterviewId"] = None
        if data.get("newSessionId"):
            context["activeSessionId"] = data["newSessionId"]
        return data
    return result


# -----------------------------------------------------------------------------
# OpenAI tool schemas
# -----------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "name": "resolve_candidate_scheduling_session",
        "description": (
            "Use this to start or restore the candidate's scheduling flow. "
            "Call this first when the candidate greets, asks to schedule, asks to see slots, "
            "or when you need to know whether an interview is already scheduled."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recruiterEmail": {"type": "string"},
                "candidateId": {"type": "string"},
                "jobId": {"type": "string"},
                "jobTitle": {"type": ["string", "null"]},
                "provider": {"type": ["string", "null"]},
                "timezone": {"type": ["string", "null"]},
                "mode": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_next_available_slots",
        "description": (
            "Use this when the candidate asks for more slots, next slots, another option, "
            "or when the current shown slots do not work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sessionId": {"type": "string"},
            },
            "required": ["sessionId"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "confirm_candidate_slot_booking",
        "description": (
            "Use this when the candidate selects one of the shown slots. "
            "Prefer selectedIndex when the candidate replies with 1, 2, or 3."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sessionId": {"type": "string"},
                "slotId": {"type": ["string", "null"]},
                "selectedIndex": {"type": ["integer", "null"], "minimum": 1, "maximum": 3},
            },
            "required": ["sessionId"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "reschedule_candidate_interview",
        "description": (
            "Use this when the candidate already has an interview scheduled and wants to change it. "
            "This cancels the old interview and returns replacement slots if available."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scheduledInterviewId": {"type": "string"},
                "requestedBy": {"type": "string"},
            },
            "required": ["scheduledInterviewId"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "cancel_candidate_interview",
        "description": (
            "Use this when the candidate wants to cancel or delete the already scheduled interview."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scheduledInterviewId": {"type": "string"},
                "cancelledBy": {"type": "string"},
            },
            "required": ["scheduledInterviewId"],
            "additionalProperties": False,
        },
    },
]

TOOL_IMPLS = {
    "resolve_candidate_scheduling_session": resolve_candidate_scheduling_session_tool,
    "get_next_available_slots": get_next_available_slots_tool,
    "confirm_candidate_slot_booking": confirm_candidate_slot_booking_tool,
    "reschedule_candidate_interview": reschedule_candidate_interview_tool,
    "cancel_candidate_interview": cancel_candidate_interview_tool,
}

# -----------------------------------------------------------------------------
# Agent runner
# -----------------------------------------------------------------------------

def _item_type(item: Any) -> str | None:
    return getattr(item, "type", None)

async def run_candidate_scheduler_agent(
    *,
    user_message: str,
    conversation_history: list[dict[str, str]],
    context: dict[str, Any],
) -> tuple[str, list[dict[str, str]], dict[str, Any]]:
    system_prompt = build_system_prompt(context)

    # Initial conversation input
    initial_input: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    initial_input.extend(conversation_history)
    initial_input.append({"role": "user", "content": user_message})

    response = client.responses.create(
        model=OPENAI_MODEL,
        input=initial_input,
        tools=TOOLS,
        temperature=0.7,
    )

    while True:
        function_calls = [item for item in response.output if _item_type(item) == "function_call"]

        # If no tool calls, we are done
        if not function_calls:
            final_text = (response.output_text or "").strip()

            updated_history = list(conversation_history)
            updated_history.append({"role": "user", "content": user_message})
            updated_history.append({"role": "assistant", "content": final_text})

            return final_text, updated_history, context

        tool_outputs = []

        for fc in function_calls:
            tool_name = fc.name
            raw_args = fc.arguments or "{}"

            try:
                parsed_args = json.loads(raw_args)
            except Exception:
                parsed_args = {}

            tool_result = await TOOL_IMPLS[tool_name](parsed_args, context)

            tool_outputs.append({
                "type": "function_call_output",
                "call_id": fc.call_id,
                "output": json.dumps(tool_result),
            })

        # IMPORTANT:
        # Continue from the exact previous response using previous_response_id
        response = client.responses.create(
            model=OPENAI_MODEL,
            previous_response_id=response.id,
            input=tool_outputs,
            tools=TOOLS,
            temperature=0.7,
        )

