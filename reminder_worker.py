from __future__ import annotations

import asyncio
import os
import socket
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from db import get_db
from email_utils import send_email_smtp
from logging_config import get_logger, setup_logging

logger = get_logger(__name__)

COLL_INTERVIEW_REMINDERS = os.getenv("COLL_INTERVIEW_REMINDERS", "interviewReminders")
COLL_SCHEDULED_INTERVIEWS = os.getenv("COLL_SCHEDULED_INTERVIEWS", "scheduledInterviews")

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
POLL_INTERVAL_SECONDS = int(os.getenv("REMINDER_WORKER_POLL_SECONDS", "30"))
BATCH_SIZE = int(os.getenv("REMINDER_WORKER_BATCH_SIZE", "25"))
PROCESSING_STALE_AFTER_SECONDS = int(os.getenv("REMINDER_PROCESSING_STALE_AFTER_SECONDS", "600"))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    raise ValueError(f"Unsupported datetime value: {value!r}")


def _normalize_whatsapp_to(number: str) -> str:
    n = (number or "").strip()
    if not n:
        raise ValueError("Empty WhatsApp recipient")
    if not n.startswith("whatsapp:"):
        n = f"whatsapp:{n}"
    return n


async def send_whatsapp_twilio(to_number: str, body: str) -> str:
    """Send a WhatsApp message using Twilio's REST API.

    Required env vars:
    - TWILIO_ACCOUNT_SID
    - TWILIO_AUTH_TOKEN
    - TWILIO_WHATSAPP_FROM   (example: whatsapp:+14155238886)
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_WHATSAPP_FROM")
    if not account_sid or not auth_token or not from_number:
        raise RuntimeError(
            "Twilio WhatsApp env vars are missing. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_WHATSAPP_FROM."
        )

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = {
        "From": from_number,
        "To": _normalize_whatsapp_to(to_number),
        "Body": body,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, data=data, auth=(account_sid, auth_token))
        resp.raise_for_status()
        payload = resp.json()
        return payload.get("sid", "")


async def send_email_async(to_email: str, subject: str, html_body: str, text_body: str) -> None:
    await asyncio.to_thread(send_email_smtp, to_email, subject, html_body, text_body)


async def get_interview_context(db, scheduled_interview_id: str) -> dict[str, Any]:
    doc = await db[COLL_SCHEDULED_INTERVIEWS].find_one(
        {"scheduledInterviewId": scheduled_interview_id},
        projection={"_id": 0},
    )
    return doc or {}



def _display_time(interview: dict[str, Any], reminder: dict[str, Any]) -> str:
    start_local = interview.get("startAtLocal")
    end_local = interview.get("endAtLocal")
    tz_name = interview.get("timezone") or reminder.get("payload", {}).get("timezone") or "Asia/Kolkata"
    if start_local and end_local:
        return f"{start_local} to {end_local} ({tz_name})"
    start_utc = interview.get("startAtUtc")
    if start_utc:
        return f"{start_utc} ({tz_name})"
    return f"upcoming interview ({tz_name})"



def build_whatsapp_body(reminder: dict[str, Any], interview: dict[str, Any]) -> str:
    payload = reminder.get("payload") or {}
    if payload.get("messageText"):
        return payload["messageText"]

    recipient_type = reminder.get("recipientType") or "candidate"
    candidate_name = interview.get("candidateName") or payload.get("candidateName") or "there"
    first_name = candidate_name.split()[0] if candidate_name else "there"
    job_title = interview.get("jobTitle") or payload.get("jobTitle")
    role_text = f" for the {job_title} role" if job_title else ""
    time_text = _display_time(interview, reminder)
    meeting_link = interview.get("meetingLink") or payload.get("meetingLink")

    if recipient_type == "recruiter":
        message = (
            f"Reminder: upcoming interview{role_text} with {candidate_name}. "
            f"Scheduled time: {time_text}."
        )
    else:
        message = f"Hi {first_name}, this is a reminder about your upcoming interview{role_text}. Time: {time_text}."

    if meeting_link:
        message += f" Meeting link: {meeting_link}"
    return message



def build_email_content(reminder: dict[str, Any], interview: dict[str, Any]) -> tuple[str, str, str]:
    payload = reminder.get("payload") or {}
    recipient_type = reminder.get("recipientType") or "candidate"
    candidate_name = interview.get("candidateName") or payload.get("candidateName") or "there"
    first_name = candidate_name.split()[0] if candidate_name else "there"
    job_title = interview.get("jobTitle") or payload.get("jobTitle")
    role_text = f" for the {job_title} role" if job_title else ""
    time_text = _display_time(interview, reminder)
    meeting_link = interview.get("meetingLink") or payload.get("meetingLink")
    template_type = reminder.get("templateType") or "interview_reminder"
    pretty_offset = template_type.replace("interview_reminder_", "").upper()
    subject = f"Reminder{f' ({pretty_offset})' if pretty_offset else ''}: Upcoming interview{role_text}"

    if recipient_type == "recruiter":
        html_body = (
            f"<p>Hi,</p>"
            f"<p>This is a reminder about the upcoming interview{role_text}.</p>"
            f"<p><strong>Candidate:</strong> {candidate_name}</p>"
            f"<p><strong>Scheduled time:</strong> {time_text}</p>"
        )
        text_body = (
            f"Hi,\n\n"
            f"This is a reminder about the upcoming interview{role_text}.\n"
            f"Candidate: {candidate_name}\n"
            f"Scheduled time: {time_text}\n"
        )
    else:
        html_body = (
            f"<p>Hi {first_name},</p>"
            f"<p>This is a reminder about your upcoming interview{role_text}.</p>"
            f"<p><strong>Scheduled time:</strong> {time_text}</p>"
        )
        text_body = (
            f"Hi {first_name},\n\n"
            f"This is a reminder about your upcoming interview{role_text}.\n"
            f"Scheduled time: {time_text}\n"
        )

    if meeting_link:
        html_body += f'<p><strong>Meeting link:</strong> <a href="{meeting_link}">{meeting_link}</a></p>'
        text_body += f"Meeting link: {meeting_link}\n"

    html_body += "<p>Please join on time.</p>"
    text_body += "Please join on time.\n"
    return subject, html_body, text_body


async def claim_due_reminder(db) -> Optional[dict[str, Any]]:
    """Atomically claim one due reminder so multiple workers do not send duplicates."""
    now = utcnow()
    stale_before = now.timestamp() - PROCESSING_STALE_AFTER_SECONDS
    # First, reset very old stuck 'processing' docs back to pending.
    await db[COLL_INTERVIEW_REMINDERS].update_many(
        {
            "status": "processing",
            "lockedAtTs": {"$lt": stale_before},
        },
        {
            "$set": {
                "status": "pending",
                "updatedAt": now,
            },
            "$unset": {"lockedAt": "", "lockedAtTs": "", "processingBy": ""},
        },
    )

    reminder = await db[COLL_INTERVIEW_REMINDERS].find_one_and_update(
        {
            "status": "pending",
            "sendAt": {"$lte": now},
        },
        {
            "$set": {
                "status": "processing",
                "lockedAt": now,
                "lockedAtTs": now.timestamp(),
                "processingBy": WORKER_ID,
                "updatedAt": now,
            }
        },
        sort=[("sendAt", 1)],
        return_document=True,
    )
    return reminder


async def mark_sent(db, reminder: dict[str, Any], provider_message_id: Optional[str] = None) -> None:
    now = utcnow()
    await db[COLL_INTERVIEW_REMINDERS].update_one(
        {"_id": reminder["_id"]},
        {
            "$set": {
                "status": "sent",
                "sentAt": now,
                "updatedAt": now,
                "providerMessageId": provider_message_id,
            },
            "$unset": {"lockedAt": "", "lockedAtTs": "", "processingBy": "", "error": ""},
        },
    )


async def mark_failed(db, reminder: dict[str, Any], exc: Exception) -> None:
    now = utcnow()
    attempt_count = int(reminder.get("attemptCount") or 0) + 1
    max_attempts = int(os.getenv("REMINDER_MAX_ATTEMPTS", "5"))
    status = "failed" if attempt_count >= max_attempts else "pending"

    update_doc = {
        "$set": {
            "status": status,
            "updatedAt": now,
            "error": str(exc),
            "attemptCount": attempt_count,
        },
        "$unset": {"lockedAt": "", "lockedAtTs": "", "processingBy": ""},
    }
    if status == "failed":
        update_doc["$set"]["failedAt"] = now
    await db[COLL_INTERVIEW_REMINDERS].update_one({"_id": reminder["_id"]}, update_doc)


async def process_single_reminder(db, reminder: dict[str, Any]) -> None:
    scheduled_interview_id = reminder.get("scheduledInterviewId")
    interview = await get_interview_context(db, scheduled_interview_id)

    # Safety: do not send reminders for cancelled / rescheduled interviews.
    interview_status = (interview.get("status") or "").lower()
    if interview and interview_status not in {"scheduled", "rescheduled", "confirmed"}:
        await db[COLL_INTERVIEW_REMINDERS].update_one(
            {"_id": reminder["_id"]},
            {
                "$set": {
                    "status": "cancelled",
                    "updatedAt": utcnow(),
                    "error": f"Interview status is {interview_status}; reminder cancelled.",
                },
                "$unset": {"lockedAt": "", "lockedAtTs": "", "processingBy": ""},
            },
        )
        return

    channel = (reminder.get("channel") or "").lower().strip()
    recipient = (reminder.get("recipient") or "").strip()
    if not recipient:
        raise ValueError("Reminder recipient is empty")

    if channel == "email":
        subject, html_body, text_body = build_email_content(reminder, interview)
        await send_email_async(recipient, subject, html_body, text_body)
        await mark_sent(db, reminder)
        logger.info("Email reminder sent reminderId=%s recipient=%s", reminder.get("reminderId"), recipient)
        return

    if channel == "whatsapp":
        body = build_whatsapp_body(reminder, interview)
        sid = await send_whatsapp_twilio(recipient, body)
        await mark_sent(db, reminder, sid)
        logger.info("WhatsApp reminder sent reminderId=%s recipient=%s sid=%s", reminder.get("reminderId"), recipient, sid)
        return

    raise ValueError(f"Unsupported reminder channel: {channel}")


async def run_once(batch_size: int = BATCH_SIZE) -> int:
    db = get_db()
    processed = 0
    for _ in range(batch_size):
        reminder = await claim_due_reminder(db)
        if not reminder:
            break
        try:
            await process_single_reminder(db, reminder)
        except Exception as exc:
            logger.exception("Reminder send failed reminderId=%s", reminder.get("reminderId"))
            await mark_failed(db, reminder, exc)
        processed += 1
    return processed


async def run_forever() -> None:
    logger.info(
        "Starting reminder worker workerId=%s poll=%ss batch=%s",
        WORKER_ID,
        POLL_INTERVAL_SECONDS,
        BATCH_SIZE,
    )
    while True:
        try:
            count = await run_once(BATCH_SIZE)
            if count:
                logger.info("Reminder worker processed=%s", count)
        except Exception:
            logger.exception("Reminder worker loop failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    setup_logging()
    mode = os.getenv("REMINDER_WORKER_MODE", "forever").strip().lower()
    if mode == "once":
        asyncio.run(run_once())
    else:
        asyncio.run(run_forever())
