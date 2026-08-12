from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.field_map import DEFAULT_REQUIRED_FIELDS, FIELDS_BY_KEY
from app.models import Strictness


class JobBase(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    department: str | None = None
    location: str | None = None
    employment_type: str | None = None
    description: str | None = None
    responsibilities: list[str] = Field(default_factory=list)

    min_years_experience: float = Field(default=0.0, ge=0)
    max_years_experience: float | None = Field(default=None, ge=0)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    education_requirement: str | None = None
    max_notice_period_days: int | None = Field(default=None, ge=0)
    max_expected_ctc: float | None = Field(default=None, ge=0)

    strictness: Strictness = Strictness.balanced
    required_fields: list[str] = Field(default_factory=lambda: list(DEFAULT_REQUIRED_FIELDS))
    screening_notes: str | None = None

    @field_validator("required_fields")
    @classmethod
    def _known_fields(cls, value: list[str]) -> list[str]:
        unknown = [key for key in value if key not in FIELDS_BY_KEY]
        if unknown:
            raise ValueError(
                f"Unknown field keys: {', '.join(unknown)}. Valid keys: {', '.join(FIELDS_BY_KEY)}"
            )
        return value


class JobCreate(JobBase):
    pass


class JobUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    department: str | None = None
    location: str | None = None
    employment_type: str | None = None
    description: str | None = None
    responsibilities: list[str] | None = None
    min_years_experience: float | None = None
    max_years_experience: float | None = None
    required_skills: list[str] | None = None
    preferred_skills: list[str] | None = None
    education_requirement: str | None = None
    max_notice_period_days: int | None = None
    max_expected_ctc: float | None = None
    strictness: Strictness | None = None
    required_fields: list[str] | None = None
    screening_notes: str | None = None
    status: Literal["draft", "open", "closed"] | None = None


class JobRead(JobBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    form_id: str | None = None
    form_url: str | None = None
    form_edit_url: str | None = None
    question_map: dict[str, str] = Field(default_factory=dict)
    created_at: dt.datetime
    updated_at: dt.datetime


class CandidateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    response_id: str
    submitted_at: dt.datetime | None = None

    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    years_experience: float | None = None
    current_role: str | None = None
    current_company: str | None = None
    skills: list[str] = Field(default_factory=list)
    education: str | None = None
    location: str | None = None
    notice_period_days: int | None = None
    expected_ctc: float | None = None
    linkedin: str | None = None
    resume_url: str | None = None
    portfolio_url: str | None = None
    cover_note: str | None = None

    status: str
    missing_fields: list[str] = Field(default_factory=list)
    rule_failures: list[str] = Field(default_factory=list)
    fit_score: int | None = None
    recommendation: str | None = None
    assessment: dict = Field(default_factory=dict)
    rationale: str | None = None
    screened_at: dt.datetime | None = None


class CreateFormRequest(BaseModel):
    field_keys: list[str] | None = Field(
        default=None,
        description="Which canonical fields to include, in order. Defaults to all of them.",
    )


class LinkFormRequest(BaseModel):
    form_ref: str = Field(description="A Google Form URL or bare form ID.")


class SendLinkRequest(BaseModel):
    recipients: list[str] = Field(min_length=1)
    subject: str | None = None
    body: str | None = None
    bcc: bool = True


class ScreenRequest(BaseModel):
    use_llm: bool = True
    rescreen: bool = False


class SyncResponse(BaseModel):
    fetched: int
    created: int
    updated: int
    unmapped_answers: int


class ScreenResponse(BaseModel):
    total: int
    shortlisted: int
    rejected_incomplete: int
    rejected_rules: int
    rejected_score: int
    errors: list[str] = Field(default_factory=list)


class FormLinkResponse(BaseModel):
    form_id: str
    form_url: str | None = None
    form_edit_url: str | None = None
    question_map: dict[str, str]
    unmapped_questions: list[dict[str, str]] = Field(default_factory=list)


class FieldInfo(BaseModel):
    key: str
    title: str
    qtype: str
    description: str
    default_required: bool


class RoundBase(BaseModel):
    name: str = Field(default="Interview Round 1", min_length=1, max_length=200)
    acceptable_score: int = Field(default=70, ge=0, le=100)
    start_date: dt.date
    day_start_time: dt.time = dt.time(10, 0)
    day_end_time: dt.time = dt.time(17, 0)
    slot_minutes: int = Field(default=30, gt=0, le=480)
    break_minutes: int = Field(default=0, ge=0, le=240)
    skip_weekends: bool = True
    timezone: str = "Asia/Kolkata"
    mode: Literal["online", "onsite", "phone"] = "online"
    difficulty: Literal["easy", "medium", "hard", "expert"] = "medium"
    location: str | None = None
    instructions: str | None = None
    contact_email: str | None = None


class RoundCreate(RoundBase):
    pass


class RoundUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    acceptable_score: int | None = Field(default=None, ge=0, le=100)
    start_date: dt.date | None = None
    day_start_time: dt.time | None = None
    day_end_time: dt.time | None = None
    slot_minutes: int | None = Field(default=None, gt=0, le=480)
    break_minutes: int | None = Field(default=None, ge=0, le=240)
    skip_weekends: bool | None = None
    timezone: str | None = None
    mode: Literal["online", "onsite", "phone"] | None = None
    difficulty: Literal["easy", "medium", "hard", "expert"] | None = None
    location: str | None = None
    instructions: str | None = None
    contact_email: str | None = None
    status: Literal["draft", "scheduled", "notified", "cancelled"] | None = None


class RoundRead(RoundBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    status: str
    created_at: dt.datetime
    updated_at: dt.datetime


class SlotRead(BaseModel):
    """What Part 3 reads to run an interview."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    round_id: int
    candidate_id: int
    scheduled_start: dt.datetime
    scheduled_end: dt.datetime
    status: str
    email_sent_at: dt.datetime | None = None
    notify_errors: dict = Field(default_factory=dict)
    candidate: CandidateRead | None = None


class ScheduleRequest(BaseModel):
    apply: bool = Field(
        default=False, description="Persist the schedule. False returns a preview only."
    )


class ScheduleResponse(BaseModel):
    applied: bool
    invited: int
    rejected: int
    unscored: int
    summary: str
    slots: list[SlotRead] = Field(default_factory=list)
    rejected_candidates: list[CandidateRead] = Field(default_factory=list)
    unscored_candidates: list[CandidateRead] = Field(default_factory=list)


class NotifyRequest(BaseModel):
    use_email: bool = True
    dry_run: bool | None = Field(
        default=None, description="Overrides NOTIFY_DRY_RUN. Null uses the configured default."
    )
    retry_all: bool = Field(
        default=False, description="Also re-send to candidates already notified."
    )


class NotifyResponse(BaseModel):
    sent: int
    partial: int
    failed: int
    skipped: int
    dry_run: bool
    errors: list[str] = Field(default_factory=list)
