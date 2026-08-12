from __future__ import annotations

import datetime as dt
from enum import Enum
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, TypeDecorator


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class UTCDateTime(TypeDecorator):
    """A datetime column that is always UTC-aware in Python.

    SQLite silently discards ``tzinfo``. A value written as 04:30+00:00 reads back as naive
    04:30, and ``astimezone()`` then treats it as *system local* time — which turned an
    interview scheduled for 10:00 IST into 04:30 IST when read from a fresh session. That is
    a wrong time in a candidate's invitation, so the conversion is pinned here at the
    storage boundary rather than trusted to every call site.

    Naive input is rejected rather than assumed: guessing the zone is how the bug started.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect) -> dt.datetime | None:  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "Refusing to store a naive datetime — attach a timezone "
                "(use datetime.now(timezone.utc), not datetime.now())."
            )
        return value.astimezone(dt.timezone.utc)

    def process_result_value(self, value: dt.datetime | None, dialect) -> dt.datetime | None:  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)


class Strictness(str, Enum):
    lenient = "lenient"
    balanced = "balanced"
    strict = "strict"
    very_strict = "very_strict"


class JobStatus(str, Enum):
    draft = "draft"
    open = "open"
    closed = "closed"


class CandidateStatus(str, Enum):
    new = "new"
    rejected_incomplete = "rejected_incomplete"
    rejected_rules = "rejected_rules"
    rejected_score = "rejected_score"
    shortlisted = "shortlisted"
    rejected_round = "rejected_round"
    scheduled = "scheduled"


ACTIVE_STATUSES = {CandidateStatus.shortlisted.value, CandidateStatus.scheduled.value}


class RoundStatus(str, Enum):
    draft = "draft"
    scheduled = "scheduled"
    notified = "notified"
    cancelled = "cancelled"


class SlotStatus(str, Enum):
    pending = "pending"
    notified = "notified"
    partly_notified = "partly_notified"
    failed = "failed"
    cancelled = "cancelled"


class InterviewMode(str, Enum):
    online = "online"
    onsite = "onsite"
    phone = "phone"


STRICTNESS_PROFILES: dict[str, dict[str, float | bool]] = {
    Strictness.lenient: {
        "score_cutoff": 45,
        "must_have_ratio": 0.34,
        "experience_slack": 2.0,
        "allow_missing_optional": True,
    },
    Strictness.balanced: {
        "score_cutoff": 62,
        "must_have_ratio": 0.60,
        "experience_slack": 1.0,
        "allow_missing_optional": True,
    },
    Strictness.strict: {
        "score_cutoff": 76,
        "must_have_ratio": 0.85,
        "experience_slack": 0.5,
        "allow_missing_optional": False,
    },
    Strictness.very_strict: {
        "score_cutoff": 87,
        "must_have_ratio": 1.0,
        "experience_slack": 0.0,
        "allow_missing_optional": False,
    },
}


class JobOpening(Base):
    """Everything the admin configures up front, before a single response arrives."""

    __tablename__ = "job_openings"

    id: Mapped[int] = mapped_column(primary_key=True)

    title: Mapped[str] = mapped_column(String(200))
    department: Mapped[str | None] = mapped_column(String(120), default=None)
    location: Mapped[str | None] = mapped_column(String(120), default=None)
    employment_type: Mapped[str | None] = mapped_column(String(60), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    responsibilities: Mapped[list[str]] = mapped_column(JSON, default=list)

    min_years_experience: Mapped[float] = mapped_column(Float, default=0.0)
    max_years_experience: Mapped[float | None] = mapped_column(Float, default=None)
    required_skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    preferred_skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    education_requirement: Mapped[str | None] = mapped_column(String(200), default=None)
    max_notice_period_days: Mapped[int | None] = mapped_column(Integer, default=None)
    max_expected_ctc: Mapped[float | None] = mapped_column(Float, default=None)

    strictness: Mapped[str] = mapped_column(String(20), default=Strictness.balanced.value)
    required_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    screening_notes: Mapped[str | None] = mapped_column(Text, default=None)

    form_id: Mapped[str | None] = mapped_column(String(120), default=None, index=True)
    form_url: Mapped[str | None] = mapped_column(String(500), default=None)
    form_edit_url: Mapped[str | None] = mapped_column(String(500), default=None)
    question_map: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(20), default=JobStatus.draft.value)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)

    candidates: Mapped[list["Candidate"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    @property
    def thresholds(self) -> dict[str, float | bool]:
        return STRICTNESS_PROFILES[Strictness(self.strictness)]

    def spec_text(self) -> str:
        """Compact, stable rendering of the role — used as the cached prompt prefix."""
        lines = [f"Job title: {self.title}"]
        if self.department:
            lines.append(f"Department: {self.department}")
        if self.location:
            lines.append(f"Location: {self.location}")
        if self.employment_type:
            lines.append(f"Employment type: {self.employment_type}")
        lines.append(f"Minimum years of experience: {self.min_years_experience}")
        if self.max_years_experience is not None:
            lines.append(f"Maximum years of experience: {self.max_years_experience}")
        lines.append("Required skills: " + (", ".join(self.required_skills) or "none specified"))
        lines.append("Preferred skills: " + (", ".join(self.preferred_skills) or "none specified"))
        if self.education_requirement:
            lines.append(f"Education requirement: {self.education_requirement}")
        if self.max_notice_period_days is not None:
            lines.append(f"Maximum acceptable notice period: {self.max_notice_period_days} days")
        if self.description:
            lines.append(f"\nRole description:\n{self.description}")
        if self.responsibilities:
            lines.append("\nResponsibilities:\n" + "\n".join(f"- {r}" for r in self.responsibilities))
        if self.screening_notes:
            lines.append(f"\nAdditional screening guidance from the hiring admin:\n{self.screening_notes}")
        return "\n".join(lines)


class Candidate(Base):
    """One Google Form response, normalised into canonical fields plus the screening verdict."""

    __tablename__ = "candidates"
    __table_args__ = (UniqueConstraint("job_id", "response_id", name="uq_candidate_response"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job_openings.id", ondelete="CASCADE"), index=True)

    response_id: Mapped[str] = mapped_column(String(200), index=True)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, default=None)

    full_name: Mapped[str | None] = mapped_column(String(200), default=None)
    email: Mapped[str | None] = mapped_column(String(200), default=None, index=True)
    phone: Mapped[str | None] = mapped_column(String(60), default=None)
    years_experience: Mapped[float | None] = mapped_column(Float, default=None)
    current_role: Mapped[str | None] = mapped_column(String(200), default=None)
    current_company: Mapped[str | None] = mapped_column(String(200), default=None)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    education: Mapped[str | None] = mapped_column(String(300), default=None)
    location: Mapped[str | None] = mapped_column(String(200), default=None)
    notice_period_days: Mapped[int | None] = mapped_column(Integer, default=None)
    expected_ctc: Mapped[float | None] = mapped_column(Float, default=None)
    linkedin: Mapped[str | None] = mapped_column(String(400), default=None)
    resume_url: Mapped[str | None] = mapped_column(String(600), default=None)
    portfolio_url: Mapped[str | None] = mapped_column(String(600), default=None)
    cover_note: Mapped[str | None] = mapped_column(Text, default=None)

    raw_response: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(30), default=CandidateStatus.new.value, index=True)
    missing_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    rule_failures: Mapped[list[str]] = mapped_column(JSON, default=list)
    fit_score: Mapped[int | None] = mapped_column(Integer, default=None)
    recommendation: Mapped[str | None] = mapped_column(String(30), default=None)
    assessment: Mapped[dict] = mapped_column(JSON, default=dict)
    rationale: Mapped[str | None] = mapped_column(Text, default=None)
    screened_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, default=None)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)

    job: Mapped[JobOpening] = relationship(back_populates="candidates")

    slots: Mapped[list["InterviewSlot"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )

    def profile_text(self) -> str:
        """Candidate rendered for the scoring prompt. Volatile half of the prompt — keep last."""
        def line(label: str, value: object) -> str | None:
            if value in (None, "", [], {}):
                return None
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            return f"{label}: {value}"

        parts = [
            line("Name", self.full_name),
            line("Years of experience", self.years_experience),
            line("Current role", self.current_role),
            line("Current company", self.current_company),
            line("Skills", self.skills),
            line("Education", self.education),
            line("Location", self.location),
            line("Notice period (days)", self.notice_period_days),
            line("Expected CTC", self.expected_ctc),
            line("LinkedIn", self.linkedin),
            line("Resume", self.resume_url),
            line("Portfolio", self.portfolio_url),
            line("Why they are a fit (their own words)", self.cover_note),
        ]
        return "\n".join(p for p in parts if p) or "(no details provided)"


class InterviewRound(Base):
    """One scheduled interview round for a job — Part 2's unit of work.

    The admin sets ``acceptable_score`` (the bar candidates must clear to be invited) and a
    scheduling window; the agent allocates slots and sends the invitations. Everything Part 3
    needs to run the interview hangs off this and its slots.
    """

    __tablename__ = "interview_rounds"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job_openings.id", ondelete="CASCADE"), index=True)

    name: Mapped[str] = mapped_column(String(200), default="Interview Round 1")
    acceptable_score: Mapped[int] = mapped_column(Integer, default=70)

    start_date: Mapped[dt.date] = mapped_column(Date)
    day_start_time: Mapped[dt.time] = mapped_column(Time, default=dt.time(10, 0))
    day_end_time: Mapped[dt.time] = mapped_column(Time, default=dt.time(17, 0))
    slot_minutes: Mapped[int] = mapped_column(Integer, default=30)
    break_minutes: Mapped[int] = mapped_column(Integer, default=0)
    skip_weekends: Mapped[bool] = mapped_column(Boolean, default=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")

    difficulty: Mapped[str] = mapped_column(String(20), default="medium")

    mode: Mapped[str] = mapped_column(String(20), default=InterviewMode.online.value)
    location: Mapped[str | None] = mapped_column(String(500), default=None)
    instructions: Mapped[str | None] = mapped_column(Text, default=None)
    contact_email: Mapped[str | None] = mapped_column(String(200), default=None)

    status: Mapped[str] = mapped_column(String(20), default=RoundStatus.draft.value)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)

    job: Mapped[JobOpening] = relationship()
    slots: Mapped[list["InterviewSlot"]] = relationship(
        back_populates="round", cascade="all, delete-orphan", order_by="InterviewSlot.scheduled_start"
    )


class InterviewSlot(Base):
    """One candidate's seat in a round: when it is, and whether they were told.

    This is the row Part 3 reads to run an interview, so it carries the confirmed time and
    the delivery audit trail rather than leaving either implicit.
    """

    __tablename__ = "interview_slots"
    __table_args__ = (UniqueConstraint("round_id", "candidate_id", name="uq_slot_round_candidate"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("interview_rounds.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )

    scheduled_start: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    scheduled_end: Mapped[dt.datetime] = mapped_column(UTCDateTime)

    status: Mapped[str] = mapped_column(String(20), default=SlotStatus.pending.value, index=True)

    email_sent_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, default=None)
    email_message_id: Mapped[str | None] = mapped_column(String(200), default=None)
    notify_errors: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)

    round: Mapped[InterviewRound] = relationship(back_populates="slots")
    candidate: Mapped[Candidate] = relationship(back_populates="slots")

    def local_start(self) -> dt.datetime:
        """Slot start rendered in the round's timezone."""
        return self.scheduled_start.astimezone(ZoneInfo(self.round.timezone))

    def local_end(self) -> dt.datetime:
        return self.scheduled_end.astimezone(ZoneInfo(self.round.timezone))
