"""Part 2 orchestration: apply the acceptable score, allocate slots, notify.

Three separable steps, deliberately not fused:

1. ``apply_score_bar``  — decide who is in and who is out
2. ``allocate_round``   — give the survivors times (nothing is sent)
3. ``notify_round``     — send the invitations

Keeping allocation and sending apart means the admin can review the full schedule before a
single message leaves, and re-running the send only retries what actually failed.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Candidate,
    CandidateStatus,
    InterviewRound,
    InterviewSlot,
    RoundStatus,
    SlotStatus,
)
from app.notify import templates
from app.notify.channels import Channel, DeliveryResult
from app.scheduling import SlotWindow, allocate_slots

logger = logging.getLogger(__name__)


class RoundError(RuntimeError):
    pass


@dataclass
class ScoreBarResult:
    invited: list[Candidate] = field(default_factory=list)
    rejected: list[Candidate] = field(default_factory=list)
    unscored: list[Candidate] = field(default_factory=list)


@dataclass
class NotifyResult:
    sent: int = 0
    partial: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _window(round_: InterviewRound) -> SlotWindow:
    return SlotWindow(
        start_date=round_.start_date,
        day_start_time=round_.day_start_time,
        day_end_time=round_.day_end_time,
        slot_minutes=round_.slot_minutes,
        break_minutes=round_.break_minutes,
        skip_weekends=round_.skip_weekends,
        timezone=round_.timezone,
    )


def apply_score_bar(session: Session, round_: InterviewRound) -> ScoreBarResult:
    """Split the job's shortlist on ``round_.acceptable_score``.

    Only Part 1 survivors are considered. A candidate rejected for missing must-have skills
    must not be revived by a score threshold, so this never reaches back past `shortlisted`.
    """
    candidates = session.scalars(
        select(Candidate)
        .where(
            Candidate.job_id == round_.job_id,
            Candidate.status.in_(
                [CandidateStatus.shortlisted.value, CandidateStatus.scheduled.value]
            ),
        )
        .order_by(Candidate.fit_score.desc().nullslast(), Candidate.id)
    ).all()

    result = ScoreBarResult()
    for candidate in candidates:
        if candidate.fit_score is None:
            result.unscored.append(candidate)
        elif candidate.fit_score >= round_.acceptable_score:
            result.invited.append(candidate)
        else:
            result.rejected.append(candidate)

    return result


def commit_score_bar(session: Session, round_: InterviewRound, outcome: ScoreBarResult) -> None:
    """Persist the rejections. Invited candidates change status only once scheduled."""
    for candidate in outcome.rejected:
        candidate.status = CandidateStatus.rejected_round.value
    session.commit()


def allocate_round(
    session: Session, round_: InterviewRound, candidates: list[Candidate]
) -> list[InterviewSlot]:
    """Create or refresh slots for ``candidates``, in the order given.

    Idempotent: a candidate who already has a slot in this round keeps their row (and its
    delivery history) and just has the time updated, so re-allocating never double-books or
    loses the audit trail.
    """
    if not candidates:
        return []

    times = allocate_slots(_window(round_), len(candidates))
    existing = {slot.candidate_id: slot for slot in round_.slots}

    slots: list[InterviewSlot] = []
    for candidate, (start, end) in zip(candidates, times, strict=True):
        slot = existing.get(candidate.id)
        if slot is None:
            slot = InterviewSlot(round_id=round_.id, candidate_id=candidate.id)
            session.add(slot)
        slot.scheduled_start = start
        slot.scheduled_end = end
        if slot.status == SlotStatus.cancelled.value:
            slot.status = SlotStatus.pending.value
        slots.append(slot)

    keep = {c.id for c in candidates}
    for candidate_id, slot in existing.items():
        if candidate_id not in keep:
            slot.status = SlotStatus.cancelled.value

    if round_.status == RoundStatus.draft.value:
        round_.status = RoundStatus.scheduled.value

    session.commit()
    session.expire(round_, ["slots"])
    return slots


def _record(slot: InterviewSlot, result: DeliveryResult, now: dt.datetime) -> None:
    errors = dict(slot.notify_errors or {})

    if result.ok and not result.skipped:
        if result.channel == "email":
            slot.email_sent_at, slot.email_message_id = now, result.message_id
        errors.pop(result.channel, None)
    elif result.error:
        errors[result.channel] = result.error

    slot.notify_errors = errors


def notify_round(
    session: Session,
    round_: InterviewRound,
    channels: list[Channel],
    *,
    only_pending: bool = True,
    join_urls: dict[int, str] | None = None,
    dry_run: bool = False,
) -> NotifyResult:
    """Send each candidate their own slot details over every channel.

    One recipient's failure never aborts the run — a bad email address must not stop the
    other forty-nine invitations. Failures are recorded per slot and summarised at the end,
    and re-running with ``only_pending`` retries just those.

    ``join_urls`` maps candidate id to their personal Part 3 interview link. Passed in
    rather than looked up here, so this module stays independent of the interview database.

    ``dry_run`` renders and reports but **persists nothing**. That distinction matters: if a
    dry run marked slots as notified, the real send afterwards would skip every one of them
    and report success while sending nothing at all.
    """
    join_urls = join_urls or {}
    result = NotifyResult()
    now = dt.datetime.now(dt.timezone.utc)
    subject = templates.invitation_subject(round_.job.title, round_)

    slots = session.scalars(
        select(InterviewSlot)
        .where(InterviewSlot.round_id == round_.id)
        .order_by(InterviewSlot.scheduled_start, InterviewSlot.id)
    ).all()

    for slot in slots:
        if slot.status == SlotStatus.cancelled.value:
            continue
        if only_pending and slot.status == SlotStatus.notified.value:
            result.skipped += 1
            continue

        candidate = slot.candidate
        outcomes: list[DeliveryResult] = []

        for channel in channels:
            link = join_urls.get(candidate.id)
            to = candidate.email or ""
            body = templates.invitation_email(candidate, slot, round_, link)

            try:
                outcome = channel.send(to, subject, body)
            except Exception as exc:
                logger.exception("Channel %s raised for candidate %s", channel.name, candidate.id)
                outcome = DeliveryResult(channel.name, ok=False, error=f"{type(exc).__name__}: {exc}")

            outcomes.append(outcome)
            if not dry_run:
                _record(slot, outcome, now)

        delivered = [o for o in outcomes if o.ok]
        hard_failures = [o for o in outcomes if not o.ok]

        if delivered and not hard_failures:
            new_status = SlotStatus.notified.value
            result.sent += 1
        elif delivered:
            new_status = SlotStatus.partly_notified.value
            result.partial += 1
        else:
            new_status = SlotStatus.failed.value
            result.failed += 1

        for outcome in hard_failures:
            label = candidate.full_name or f"candidate {candidate.id}"
            result.errors.append(f"{label} [{outcome.channel}]: {outcome.error}")

        if dry_run:
            continue

        slot.status = new_status
        if new_status in (SlotStatus.notified.value, SlotStatus.partly_notified.value):
            candidate.status = CandidateStatus.scheduled.value

    if dry_run:
        session.rollback()
        return result

    if result.sent or result.partial:
        round_.status = RoundStatus.notified.value

    session.commit()
    return result
