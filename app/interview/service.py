"""Orchestration across the two databases.

Every path that pairs an interview with a person goes through here, because the one failure
this system must never have is interviewing someone against another candidate's resume.
``resolve_interview`` re-checks the identity snapshot on every single load and refuses on a
mismatch rather than carrying on with plausible-looking data.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.interview.models import (
    Difficulty,
    Interview,
    InterviewStatus,
    ProctoringEvent,
    Speaker,
    TranscriptTurn,
    ViolationType,
)
from app.models import Candidate, InterviewRound, InterviewSlot, JobOpening, SlotStatus

logger = logging.getLogger(__name__)


class InterviewError(RuntimeError):
    pass


class IdentityMismatch(InterviewError):
    """The stored interview no longer matches the candidate it claims to belong to."""


def _token() -> str:
    return secrets.token_urlsafe(24)


def join_url(interview: Interview) -> str:
    base = get_settings().interview_base_url.rstrip("/")
    return f"{base}/interview/{interview.access_token}"


def prepare_interviews(
    main: Session,
    interviews: Session,
    round_: InterviewRound,
    *,
    plan_questions: bool = True,
    fetch_resumes: bool = True,
) -> list[Interview]:
    """Create an Interview for every active slot in the round.

    Idempotent — a slot that already has an interview keeps it (and its token, so a link
    already emailed to a candidate never stops working).
    """
    from app.interview.plan import build_question_plan, fallback_plan
    from app.interview.resume import ResumeFetchError, build_dossier, fetch_resume_text

    job = main.get(JobOpening, round_.job_id)
    if job is None:
        raise InterviewError(f"Job {round_.job_id} not found.")

    slots = main.scalars(
        select(InterviewSlot)
        .where(
            InterviewSlot.round_id == round_.id,
            InterviewSlot.status != SlotStatus.cancelled.value,
        )
        .order_by(InterviewSlot.scheduled_start)
    ).all()

    existing = {
        row.slot_id: row
        for row in interviews.scalars(
            select(Interview).where(Interview.round_id == round_.id)
        ).all()
    }

    prepared: list[Interview] = []
    for slot in slots:
        candidate = main.get(Candidate, slot.candidate_id)
        if candidate is None:
            raise InterviewError(f"Slot {slot.id} points at missing candidate {slot.candidate_id}.")

        interview = existing.get(slot.id)
        if interview is None:
            interview = Interview(
                slot_id=slot.id,
                candidate_id=candidate.id,
                round_id=round_.id,
                job_id=job.id,
                access_token=_token(),
            )
            interviews.add(interview)

        interview.candidate_name = candidate.full_name
        interview.candidate_email = candidate.email
        interview.difficulty = round_.difficulty
        interview.scheduled_start = slot.scheduled_start
        interview.resume_snapshot = build_dossier(candidate, job)

        if fetch_resumes and not interview.resume_text:
            try:
                interview.resume_text = fetch_resume_text(candidate.resume_url)
            except ResumeFetchError as exc:
                logger.info("Resume not fetched for candidate %s: %s", candidate.id, exc)
                interview.resume_text = None

        if plan_questions and not interview.question_plan:
            try:
                plan, provider = build_question_plan(
                    interview.resume_snapshot, interview.difficulty, interview.resume_text
                )
            except Exception as exc:
                logger.warning("Planner failed for candidate %s (%s); using fallback", candidate.id, exc)
                plan, provider = fallback_plan(interview.resume_snapshot, interview.difficulty), "offline"
            interview.question_plan = plan.model_dump()
            interview.providers = {**(interview.providers or {}), "plan": provider}

        prepared.append(interview)

    interviews.commit()
    return prepared


def resolve_interview(main: Session, interviews: Session, token: str) -> tuple[Interview, Candidate]:
    """Load an interview by join token and prove it belongs to the candidate it names.

    Three checks, all of which must pass: the interview exists, the candidate exists, and
    the stored identity snapshot still matches. Anything else is refused — a stale or
    tampered link must not open someone else's interview.
    """
    interview = interviews.scalars(
        select(Interview).where(Interview.access_token == token)
    ).first()
    if interview is None:
        raise InterviewError("This interview link is not valid.")

    candidate = main.get(Candidate, interview.candidate_id)
    if candidate is None:
        raise IdentityMismatch(
            f"Interview {interview.id} references candidate {interview.candidate_id}, "
            "which no longer exists."
        )

    snapshot_id = (interview.resume_snapshot or {}).get("candidate_id")
    if snapshot_id is not None and snapshot_id != candidate.id:
        raise IdentityMismatch(
            f"Interview {interview.id} carries a dossier for candidate {snapshot_id} but is "
            f"linked to candidate {candidate.id}. Refusing to run the wrong interview."
        )

    if interview.candidate_email and candidate.email and interview.candidate_email != candidate.email:
        raise IdentityMismatch(
            f"Interview {interview.id} was prepared for {interview.candidate_email} but "
            f"candidate {candidate.id} is now {candidate.email}. Re-run prepare."
        )

    return interview, candidate


def start_interview(interviews: Session, interview: Interview) -> Interview:
    if interview.status in (InterviewStatus.completed.value, InterviewStatus.graded.value):
        raise InterviewError("This interview has already been completed.")
    if interview.status == InterviewStatus.terminated.value:
        raise InterviewError("This interview was ended early and cannot be resumed.")

    if interview.started_at is None:
        interview.started_at = dt.datetime.now(dt.timezone.utc)
    interview.status = InterviewStatus.in_progress.value
    interviews.commit()
    return interview


def record_turn(
    interviews: Session, interview: Interview, speaker: str, text: str, at_seconds: float
) -> TranscriptTurn | None:
    """Append one utterance. Blank text is dropped — partial transcripts arrive empty."""
    if not text or not text.strip():
        return None

    next_sequence = (
        interviews.scalar(
            select(TranscriptTurn.sequence)
            .where(TranscriptTurn.interview_id == interview.id)
            .order_by(TranscriptTurn.sequence.desc())
            .limit(1)
        )
        or 0
    ) + 1

    turn = TranscriptTurn(
        interview_id=interview.id,
        sequence=next_sequence,
        speaker=Speaker(speaker).value,
        text=text.strip(),
        at_seconds=max(0.0, at_seconds),
    )
    interviews.add(turn)
    interviews.commit()
    return turn


def record_violation(
    interviews: Session,
    interview: Interview,
    kind: str,
    at_seconds: float,
    duration_seconds: float,
    detail: str | None = None,
) -> tuple[ProctoringEvent, bool]:
    """Record a proctoring event and report whether the call should now end.

    Sub-threshold observations are stored with ``counted=False``: useful context for a
    reviewer, but they never push a candidate toward termination.
    """
    settings = get_settings()
    thresholds = {
        ViolationType.looked_away.value: settings.proctor_look_away_seconds,
        ViolationType.no_face.value: settings.proctor_no_face_seconds,
        ViolationType.tab_hidden.value: settings.proctor_tab_hidden_seconds,
        ViolationType.window_blurred.value: settings.proctor_tab_hidden_seconds,
    }
    counted = duration_seconds >= thresholds.get(kind, 0.0)

    event = ProctoringEvent(
        interview_id=interview.id,
        kind=kind,
        at_seconds=max(0.0, at_seconds),
        duration_seconds=max(0.0, duration_seconds),
        counted=counted,
        detail=detail,
    )
    interviews.add(event)

    if counted:
        interview.violation_count = (interview.violation_count or 0) + 1

    interviews.commit()
    should_end = (interview.violation_count or 0) >= settings.proctor_max_violations
    return event, should_end


def end_interview(
    interviews: Session, interview: Interview, reason: str = "completed"
) -> Interview:
    if interview.ended_at is None:
        interview.ended_at = dt.datetime.now(dt.timezone.utc)
    interview.end_reason = reason
    interview.status = (
        InterviewStatus.terminated.value
        if reason.startswith("proctor")
        else InterviewStatus.completed.value
    )

    counted = [e for e in interview.events if e.counted]
    if counted:
        kinds: dict[str, int] = {}
        for event in counted:
            kinds[event.kind] = kinds.get(event.kind, 0) + 1
        interview.integrity_note = "; ".join(f"{k} x{v}" for k, v in sorted(kinds.items()))

    interviews.commit()
    return interview


def grade_and_store(interviews: Session, interview: Interview) -> Interview:
    """Run the grader over the stored transcript and save the report."""
    from app.interview.grading import grade_interview

    if not interview.turns:
        raise InterviewError("There is no transcript to grade.")

    report, provider = grade_interview(interview)
    interview.providers = {**(interview.providers or {}), "grading": provider}
    interview.overall_rating = report.overall_rating
    interview.recommendation = report.recommendation
    interview.summary = report.summary
    interview.ratings = report.model_dump()
    interview.graded_at = dt.datetime.now(dt.timezone.utc)
    interview.status = InterviewStatus.graded.value
    interviews.commit()
    return interview


def set_round_difficulty(main: Session, round_: InterviewRound, difficulty: str) -> InterviewRound:
    round_.difficulty = Difficulty(difficulty).value
    main.commit()
    return round_
