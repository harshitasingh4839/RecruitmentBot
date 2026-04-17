from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from typing import Any, Dict, Literal, Optional, List

Provider = Literal["google", "microsoft"]

# Request and response models for the check calendar connection endpoint
class CheckCalendarConnectionRequest(BaseModel):
    recruiterEmail: EmailStr
    provider: Provider = Field(default="google")

class CheckCalendarConnectionResponse(BaseModel):
    message: str
    status: bool

# Request and response models for starting Google OAuth flow
class StartGoogleOAuthRequest(BaseModel):
    recruiterEmail: EmailStr
    provider: Provider = Field(default="google")

class StartGoogleOAuthResponse(BaseModel):
    recruiterEmail: EmailStr
    provider: Provider
    authUrl: str
    message: str

# Request and response models for Google FreeBusy endpoint
class GetGoogleFreeBusyRequest(BaseModel):
    recruiterEmail: EmailStr
    provider: Provider = Field(default="google")
    timeMin: str  # ISO8601, e.g. "2026-03-02T00:00:00+05:30" or "...Z"
    timeMax: str  # ISO8601
    timezone: str = Field(default="Asia/Kolkata")
    calendarId: str = Field(default="primary")  # usually "primary"

class BusyInterval(BaseModel):
    start: str  # ISO8601 from Google (RFC3339)
    end: str

class GetGoogleFreeBusyResponse(BaseModel):
    recruiterEmail: EmailStr
    provider: Provider
    calendarId: str
    timeMin: str
    timeMax: str
    busy: List[BusyInterval]
    message: str
    usedTokenRefresh: bool = False

# Request and response models for proposing slots
class TimeWindow(BaseModel):
    start: str  # "HH:MM"
    end: str    # "HH:MM"

class DayAvailability(BaseModel):
    date: str  # "YYYY-MM-DD"
    windows: List[TimeWindow]

class Slot(BaseModel):
    startAtLocal: str  # "YYYY-MM-DDTHH:MM"
    endAtLocal: str
    startAtUtc: str    # "YYYY-MM-DDTHH:MM:SSZ"
    endAtUtc: str

# Note: for simplicity, we are not modeling the "proposal" as a first-class entity in the schema. The proposalId is just an opaque string that links the propose and save endpoints.
class ProposeSlotsRequest(BaseModel):
    recruiterEmail: EmailStr
    provider: Provider = Field(default="google")
    timezone: str = Field(default="Asia/Kolkata")
    # jobId: Optional[str] = None
    jobId: str = Field(min_length=1)
    jobTitle: Optional[str] = None

    # You can pass availability in two styles:
    availability: Optional[List[DayAvailability]] = None  # explicit per-day windows
    # OR: if you want later, you can add date range + common windows; for now we support explicit availability.

    slotDurationMinutes: int = Field(default=30, ge=5, le=240)
    bufferMinutes: int = Field(default=0, ge=0, le=120)

    # Optional: busy intervals from getGoogleFreeBusy
    busy: Optional[List[BusyInterval]] = None

    # Optional: meeting preference metadata (not used in generation)
    mode: Optional[str] = Field(default="google_meet")

    @field_validator("jobId")
    @classmethod
    def validate_job_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("jobId must not be empty")
        return v

    @field_validator("jobTitle")
    @classmethod
    def normalize_job_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

# The response includes the generated slots and a human-friendly preview text that can be directly shown in the chat.
class ProposeSlotsResponse(BaseModel):
    recruiterEmail: EmailStr
    provider: Provider
    timezone: str
    slotDurationMinutes: int
    bufferMinutes: int
    proposalId: Optional[str] = None
    previewText: Optional[str] = None
    slots: List[Slot]
    summary: str
    warnings: List[str] = []
    needs_clarification: Optional[str] = None

# Request and response models for saving proposed slots into final availability collection
class SaveSlotsRequest(BaseModel):
    recruiterEmail: EmailStr
    provider: Provider = Field(default="google")
    proposalId: str
    # jobId: Optional[str] = None
    jobId: str = Field(min_length=1)
    jobTitle: Optional[str] = None
    mode: Optional[str] = Field(default="google_meet")

    @field_validator("jobId")
    @classmethod
    def validate_job_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("jobId must not be empty")
        return v

    @field_validator("jobTitle")
    @classmethod
    def normalize_job_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

class SaveSlotsResponse(BaseModel):
    recruiterEmail: EmailStr
    provider: Provider
    proposalId: str
    savedCount: int
    message: str

class CandidateSlotOption(BaseModel):
    slotId: str
    displayText: str
    startAtUtc: Optional[str] = None
    endAtUtc: Optional[str] = None
    startAtLocal: Optional[str] = None
    endAtLocal: Optional[str] = None

# Request and response models for getting next available slots in candidate scheduling session
class GetNextAvailableSlotsRequest(BaseModel):
    sessionId: str = Field(min_length=1)

class GetNextAvailableSlotsResponse(BaseModel):
    sessionId: str
    candidateId: str
    recruiterEmail: EmailStr
    jobId: str
    jobTitle: Optional[str] = None
    slots: List[CandidateSlotOption]
    messageText: str
    hasMore: bool
    nextAction: Literal["show_slots", "no_more_slots"] = "show_slots"
    availableActions: List[str] = []
    message: str

# Request and response models for confirming a candidate slot booking
class ConfirmCandidateSlotBookingRequest(BaseModel):
    sessionId: str = Field(min_length=1)
    slotId: Optional[str] = None
    selectedIndex: Optional[int] = Field(default=None, ge=1, le=3)

    @field_validator("sessionId")
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("sessionId must not be empty")
        return v

    @field_validator("slotId")
    @classmethod
    def validate_slot_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @model_validator(mode="after")
    def validate_selection(self):
        if not self.slotId and self.selectedIndex is None:
            raise ValueError("Provide either slotId or selectedIndex.")
        return self

class ConfirmCandidateSlotBookingResponse(BaseModel):
    sessionId: str
    candidateId: str
    recruiterEmail: EmailStr
    jobId: str
    jobTitle: Optional[str] = None
    scheduledInterviewId: str
    slotId: str
    meetingLink: Optional[str] = None
    startAtUtc: Optional[str] = None
    endAtUtc: Optional[str] = None
    startAtLocal: Optional[str] = None
    endAtLocal: Optional[str] = None
    messageText: str
    reminderCount: int = 0
    message: str

# Request and response models for cancelling a candidate interview
class CancelCandidateInterviewRequest(BaseModel):
    scheduledInterviewId: str = Field(min_length=1)
    cancelledBy: str = Field(default="candidate")

class CancelCandidateInterviewResponse(BaseModel):
    scheduledInterviewId: str
    sessionId: Optional[str] = None
    candidateId: str
    recruiterEmail: EmailStr
    jobId: str
    jobTitle: Optional[str] = None
    slotId: str
    slotReopened: bool
    cancelledReminderCount: int
    messageText: str
    message: str

# Request and response models for rescheduling a candidate interview 
class RescheduleCandidateInterviewRequest(BaseModel):
    scheduledInterviewId: str = Field(min_length=1)
    requestedBy: str = Field(default="candidate")

    @field_validator("scheduledInterviewId", "requestedBy")
    @classmethod
    def validate_required_fields(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("value must not be empty")
        return v

class RescheduleCandidateInterviewResponse(BaseModel):
    oldScheduledInterviewId: str
    newSessionId: Optional[str] = None
    candidateId: str
    recruiterEmail: EmailStr
    jobId: str
    jobTitle: Optional[str] = None
    oldSlotReopened: bool
    cancelledReminderCount: int
    slots: List[CandidateSlotOption]
    messageText: str
    hasMore: bool
    nextAction: Literal["show_slots", "no_slots_available"] = "show_slots"
    availableActions: List[str] = []
    message: str

# Request and response models for starting the reschedule flow
class StartCandidateRescheduleRequest(BaseModel):
    scheduledInterviewId: str = Field(min_length=1)
    requestedBy: str = Field(default="candidate")

    @field_validator("scheduledInterviewId", "requestedBy")
    @classmethod
    def validate_required_fields(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("value must not be empty")
        return v

class StartCandidateRescheduleRequestResponse(BaseModel):
    oldScheduledInterviewId: str
    sessionId: Optional[str] = None
    candidateId: str
    recruiterEmail: EmailStr
    jobId: str
    jobTitle: Optional[str] = None
    slots: List[CandidateSlotOption]
    messageText: str
    hasMore: bool
    nextAction: Literal["show_slots", "no_slots_available"] = "show_slots"
    availableActions: List[str] = []
    message: str

# Request and response models for creating a reschedule request (which the recruiter can then approve or reject)
class CreateCandidateRescheduleRequestRequest(BaseModel):
    sessionId: str = Field(min_length=1)
    slotId: Optional[str] = None
    selectedIndex: Optional[int] = Field(default=None, ge=1, le=3)

    @field_validator("sessionId")
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("sessionId must not be empty")
        return v

    @field_validator("slotId")
    @classmethod
    def validate_slot_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @model_validator(mode="after")
    def validate_selection(self):
        if not self.slotId and self.selectedIndex is None:
            raise ValueError("Provide either slotId or selectedIndex.")
        return self

class CreateCandidateRescheduleRequestResponse(BaseModel):
    requestId: str
    sessionId: str
    oldScheduledInterviewId: str
    candidateId: str
    recruiterEmail: EmailStr
    jobId: str
    jobTitle: Optional[str] = None
    requestedSlotId: str
    requestedSlotDisplayText: str
    requestStatus: str
    messageText: str
    availableActions: List[str] = []
    message: str

# Request and response models for recruiter approving or rejecting a reschedule request
class ApproveRescheduleRequestRequest(BaseModel):
    requestId: str = Field(min_length=1)
    reviewedBy: str = Field(min_length=1)

class ApproveRescheduleRequestResponse(BaseModel):
    requestId: str
    oldScheduledInterviewId: str
    newScheduledInterviewId: str
    candidateId: str
    recruiterEmail: EmailStr
    jobId: str
    jobTitle: Optional[str] = None
    approved: bool
    messageText: str
    message: str

# Request and response models for rejecting a reschedule request (with optional reason)
class RejectRescheduleRequestRequest(BaseModel):
    requestId: str = Field(min_length=1)
    reviewedBy: str = Field(min_length=1)
    reason: Optional[str] = None

class RejectRescheduleRequestResponse(BaseModel):
    requestId: str
    oldScheduledInterviewId: str
    candidateId: str
    recruiterEmail: EmailStr
    jobId: str
    jobTitle: Optional[str] = None
    rejected: bool
    messageText: str
    message: str

# Response model for listing reschedule requests in the recruiter dashboard (can be used for both pending and past requests)
class RescheduleRequestDashboardItem(BaseModel):
    requestId: str
    scheduledInterviewId: str
    candidateId: str
    candidateName: Optional[str] = None
    recruiterEmail: EmailStr
    jobId: str
    jobTitle: Optional[str] = None
    requestStatus: str
    requestedSlotId: str
    requestedSlotDisplayText: Optional[str] = None
    requestedAt: Optional[str] = None
    reviewedAt: Optional[str] = None
    reviewedBy: Optional[str] = None
    reviewComment: Optional[str] = None

class ListRescheduleRequestsResponse(BaseModel):
    items: List[RescheduleRequestDashboardItem]
    message: str

# Request and response models for resolving candidate scheduling session (used when candidate clicks on "reschedule" link in calendar invite or email, to show them the available slots and actions)
class ResolveCandidateSchedulingSessionRequest(BaseModel):
    recruiterEmail: EmailStr
    candidateId: str = Field(min_length=1)
    jobId: str = Field(min_length=1)
    jobTitle: Optional[str] = None
    provider: Provider = Field(default="google")
    timezone: str = Field(default="Asia/Kolkata")
    mode: Optional[str] = Field(default="google_meet")

    @field_validator("candidateId")
    @classmethod
    def validate_candidate_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("candidateId must not be empty")
        return v

    @field_validator("jobId")
    @classmethod
    def validate_job_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("jobId must not be empty")
        return v

    @field_validator("jobTitle")
    @classmethod
    def normalize_job_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None


class ResolveCandidateSchedulingSessionResponse(BaseModel):
    nextAction: Literal[
        "already_scheduled",
        "continue_session",
        "new_session_created",
        "no_slots_available"
    ]
    sessionId: Optional[str] = None
    scheduledInterviewId: Optional[str] = None
    candidateId: str
    recruiterEmail: EmailStr
    jobId: str
    jobTitle: Optional[str] = None
    slots: List[CandidateSlotOption] = []
    hasMore: bool = False
    availableActions: List[str] = []
    messageText: str
    message: str


# Schema for agent_server endpoints
from typing import Literal, Optional, List, Dict, Any
from pydantic import BaseModel, EmailStr, Field, field_validator

class RunCandidateWhatsappAgentRequest(BaseModel):
    recruiterEmail: EmailStr
    candidateEmail: EmailStr
    jobId: str = Field(min_length=1)
    jobTitle: Optional[str] = None
    userPrompt: Optional[str] = None
    triggerType: Literal["outbound_start", "candidate_reply"] = "candidate_reply"
    provider: str = "google"
    timezone: str = "Asia/Kolkata"
    mode: Optional[str] = "google_meet"

    @field_validator("jobId")
    @classmethod
    def validate_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("value must not be empty")
        return v

    @field_validator("jobTitle")
    @classmethod
    def normalize_job_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("userPrompt")
    @classmethod
    def normalize_prompt(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

class RunCandidateWhatsappAgentResponse(BaseModel):
    replyText: str
    candidateId: str
    candidateEmail: EmailStr
    recruiterEmail: EmailStr
    jobId: str
    jobTitle: Optional[str] = None
    sessionId: Optional[str] = None
    scheduledInterviewId: Optional[str] = None
    availableActions: List[str] = []
    nextAction: Optional[str] = None
    message: str

# Request and response models for saving direct slots (bypassing proposal flow)
class DirectSlotItem(BaseModel):
    startAtLocal: str
    endAtLocal: str

class SaveDirectSlotsRequest(BaseModel):
    recruiterEmail: EmailStr
    slots: List[DirectSlotItem] = Field(..., min_length=1)
    provider: str = "google"
    timezone: str = "Asia/Kolkata"
    mode: Optional[str] = "google_meet"

class SaveDirectSlotsResponse(BaseModel):
    recruiterEmail: str
    provider: str
    savedCount: int
    message: str

# Request model for candidate and recruiter login
class CandidateLoginRequest(BaseModel):
    name: str
    email: EmailStr
    phone: str

class CandidateSelectedJob(BaseModel):
    jobId: str = Field(min_length=1)
    jobTitle: Optional[str] = None
    recruiterEmail: Optional[EmailStr] = None
    # metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("jobId")
    @classmethod
    def validate_job_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("jobId must not be empty")
        return v

    @field_validator("jobTitle")
    @classmethod
    def normalize_optional_text(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

class CandidateJobSelectionRequest(BaseModel):
    email: str
    selectedJob: CandidateSelectedJob

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        v = v.lower().strip()
        if not v:
            raise ValueError("email is required for candidate lookup")
        return v

    @model_validator(mode="after")
    def require_candidate_lookup(self):
        if not self.email:
            raise ValueError("email is required for candidate lookup")
        return self

class RecruiterLoginRequest(BaseModel):
    name: str
    email: EmailStr
    phone: str
