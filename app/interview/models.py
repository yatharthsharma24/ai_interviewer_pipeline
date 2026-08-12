"""Interview records — transcripts, ratings, and proctoring events.

These live in their **own database** (``INTERVIEW_DATABASE_URL``, default
``data/interviews.db``), separate from the candidate/resume database. That is a deliberate
split: interview recordings are far more sensitive than an application form, and keeping
them in a distinct file makes them separately backed up, separately encrypted, and
separately deletable when a retention policy says so.

The cost of the split is that SQLite cannot enforce a foreign key across files, so
``candidate_id`` / ``slot_id`` are plain integers validated in code rather than by the
engine. Every read path goes through ``app.interview.service``, which resolves them against
the main database and refuses to proceed on a mismatch — an interview attached to the wrong
resume is the one failure this design must never allow.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum

from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.models import UTCDateTime, _utcnow


class InterviewBase(DeclarativeBase):
    """Separate metadata so this schema is created in its own database file."""


class Difficulty(str, Enum):
    easy = "easy"
    medium = "medium"
    hard = "hard"
    expert = "expert"


DIFFICULTY_PROFILES: dict[str, dict[str, object]] = {
    Difficulty.easy: {
        "questions": 5,
        "follow_up_depth": 1,
        "guidance": (
            "Keep questions concrete and close to what the candidate has actually done. "
            "Accept a reasonable answer and move on. Offer a hint if they stall."
        ),
    },
    Difficulty.medium: {
        "questions": 7,
        "follow_up_depth": 2,
        "guidance": (
            "Ask for specifics behind their claims. Follow up once when an answer is vague. "
            "Expect them to justify a design choice, but do not push into trivia."
        ),
    },
    Difficulty.hard: {
        "questions": 8,
        "follow_up_depth": 3,
        "guidance": (
            "Probe until you reach the edge of their knowledge. Challenge assumptions, ask "
            "how their approach fails under load or at scale, and follow up on hand-waving."
        ),
    },
    Difficulty.expert: {
        "questions": 9,
        "follow_up_depth": 4,
        "guidance": (
            "Interview at senior-staff level. Press hard on trade-offs, failure modes, and "
            "what they would do differently. Do not accept an answer without a reason."
        ),
    },
}


class InterviewStatus(str, Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    terminated = "terminated"
    no_show = "no_show"
    graded = "graded"


class Speaker(str, Enum):
    interviewer = "interviewer"
    candidate = "candidate"
    system = "system"


class ViolationType(str, Enum):
    tab_hidden = "tab_hidden"
    window_blurred = "window_blurred"
    looked_away = "looked_away"
    no_face = "no_face"
    multiple_faces = "multiple_faces"
    camera_off = "camera_off"
    mic_off = "mic_off"
    fullscreen_exited = "fullscreen_exited"


class Interview(InterviewBase):
    """One interview: who, when, what was asked, what was said, and how it scored."""

    __tablename__ = "interviews"
    __table_args__ = (UniqueConstraint("slot_id", name="uq_interview_slot"),)

    id: Mapped[int] = mapped_column(primary_key=True)

    slot_id: Mapped[int] = mapped_column(Integer, index=True)
    candidate_id: Mapped[int] = mapped_column(Integer, index=True)
    round_id: Mapped[int] = mapped_column(Integer, index=True)
    job_id: Mapped[int] = mapped_column(Integer, index=True)

    candidate_name: Mapped[str | None] = mapped_column(String(200), default=None)
    candidate_email: Mapped[str | None] = mapped_column(String(200), default=None)

    access_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    difficulty: Mapped[str] = mapped_column(String(20), default=Difficulty.medium.value)
    status: Mapped[str] = mapped_column(
        String(20), default=InterviewStatus.pending.value, index=True
    )

    resume_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    resume_text: Mapped[str | None] = mapped_column(Text, default=None)
    question_plan: Mapped[list] = mapped_column(JSON, default=list)

    scheduled_start: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, default=None)
    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, default=None)
    ended_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, default=None)
    end_reason: Mapped[str | None] = mapped_column(String(200), default=None)

    overall_rating: Mapped[int | None] = mapped_column(Integer, default=None)
    recommendation: Mapped[str | None] = mapped_column(String(30), default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    ratings: Mapped[dict] = mapped_column(JSON, default=dict)
    graded_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, default=None)

    violation_count: Mapped[int] = mapped_column(Integer, default=0)
    integrity_note: Mapped[str | None] = mapped_column(Text, default=None)

    providers: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)

    turns: Mapped[list["TranscriptTurn"]] = relationship(
        back_populates="interview",
        cascade="all, delete-orphan",
        order_by="TranscriptTurn.sequence",
    )
    events: Mapped[list["ProctoringEvent"]] = relationship(
        back_populates="interview",
        cascade="all, delete-orphan",
        order_by="ProctoringEvent.at_seconds",
    )

    @property
    def profile(self) -> dict:
        return DIFFICULTY_PROFILES[Difficulty(self.difficulty)]

    def transcript_text(self) -> str:
        """The whole conversation as plain text — what the grader reads."""
        lines = []
        for turn in self.turns:
            who = "INTERVIEWER" if turn.speaker == Speaker.interviewer.value else "CANDIDATE"
            if turn.speaker == Speaker.system.value:
                who = "SYSTEM"
            stamp = f"[{int(turn.at_seconds // 60):02d}:{int(turn.at_seconds % 60):02d}]"
            lines.append(f"{stamp} {who}: {turn.text}")
        return "\n".join(lines) or "(no transcript recorded)"

    def duration_seconds(self) -> float | None:
        if not self.started_at or not self.ended_at:
            return None
        return (self.ended_at - self.started_at).total_seconds()


class TranscriptTurn(InterviewBase):
    """One utterance. Stored per turn rather than as a blob so the summary can cite times."""

    __tablename__ = "transcript_turns"

    id: Mapped[int] = mapped_column(primary_key=True)
    interview_id: Mapped[int] = mapped_column(
        ForeignKey("interviews.id", ondelete="CASCADE"), index=True
    )

    sequence: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    at_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)

    interview: Mapped[Interview] = relationship(back_populates="turns")


class ProctoringEvent(InterviewBase):
    """A single integrity observation, with when it happened and how long it lasted."""

    __tablename__ = "proctoring_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    interview_id: Mapped[int] = mapped_column(
        ForeignKey("interviews.id", ondelete="CASCADE"), index=True
    )

    kind: Mapped[str] = mapped_column(String(30), index=True)
    at_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    counted: Mapped[bool] = mapped_column(Integer, default=True)
    detail: Mapped[str | None] = mapped_column(String(400), default=None)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)

    interview: Mapped[Interview] = relationship(back_populates="events")
