from pydantic import BaseModel, EmailStr, Field
from typing import Literal, Optional, Literal, List

Provider = Literal["google", "microsoft"]

# Request and response models for the check calendar connection endpoint
class CheckCalendarConnectionRequest(BaseModel):
    recruiterEmail: EmailStr
    provider: Provider = Field(default="google")

class CheckCalendarConnectionResponse(BaseModel):
    message: str

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
    jobId: Optional[str] = None

    # You can pass availability in two styles:
    availability: Optional[List[DayAvailability]] = None  # explicit per-day windows
    # OR: if you want later, you can add date range + common windows; for now we support explicit availability.

    slotDurationMinutes: int = Field(default=30, ge=5, le=240)
    bufferMinutes: int = Field(default=0, ge=0, le=120)

    # Optional: busy intervals from getGoogleFreeBusy
    busy: Optional[List[BusyInterval]] = None

    # Optional: meeting preference metadata (not used in generation)
    mode: Optional[str] = Field(default="google_meet")

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
    jobId: Optional[str] = None
    mode: Optional[str] = None

class SaveSlotsResponse(BaseModel):
    recruiterEmail: EmailStr
    provider: Provider
    proposalId: str
    savedCount: int
    message: str