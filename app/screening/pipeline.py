"""Orchestration: pull responses out of Google, normalise them, filter, then score.

``sync_responses`` is idempotent — it keys on Google's ``responseId``, so running it on a
schedule only ever adds or refreshes rows.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.field_map import FIELDS_BY_KEY
from app.google_api import forms as forms_api
from app.models import Candidate, CandidateStatus, JobOpening
from app.parsing import (
    clean_text,
    parse_currency,
    parse_list,
    parse_notice_period_days,
    parse_years,
)
from app.screening.llm import ScoringError, ScoringRefusal, score_candidate
from app.screening.rules import apply_rules

logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    pass


@dataclass
class SyncResult:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    unmapped_answers: int = 0


@dataclass
class ScreenResult:
    total: int = 0
    shortlisted: int = 0
    rejected_incomplete: int = 0
    rejected_rules: int = 0
    rejected_score: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


_COERCERS = {
    "years_experience": parse_years,
    "notice_period_days": parse_notice_period_days,
    "expected_ctc": parse_currency,
}


def _coerce(key: str, values: list[str]):
    if key == "skills":
        return parse_list(values)

    joined = clean_text(", ".join(v for v in values if v))
    coercer = _COERCERS.get(key)
    if coercer:
        return coercer(joined)
    return joined


def response_to_fields(response: dict, question_map: dict[str, str]) -> tuple[dict, int]:
    """Map one Google response onto canonical candidate columns.

    Returns ``(fields, unmapped_count)``. Answers to questions we do not recognise are not
    dropped — the caller still stores the whole raw payload.
    """
    by_question_id = {qid: key for key, qid in question_map.items()}
    answers = response.get("answers", {}) or {}

    fields: dict[str, object] = {}
    unmapped = 0

    for question_id, answer in answers.items():
        key = by_question_id.get(question_id)
        values = forms_api.answer_values(answer)
        if key is None:
            unmapped += 1
            continue
        if key not in FIELDS_BY_KEY:
            unmapped += 1
            continue
        fields[key] = _coerce(key, values)

    return fields, unmapped


def sync_responses(session: Session, job: JobOpening) -> SyncResult:
    """Fetch every response for ``job``'s form and upsert it as a Candidate row."""
    if not job.form_id:
        raise PipelineError(f"Job {job.id} has no linked Google Form. Run create-form or link-form.")
    if not job.question_map:
        raise PipelineError(
            f"Job {job.id} has no question map, so answers cannot be mapped to fields. "
            "Run link-form to (re-)inspect the form."
        )

    responses = forms_api.list_responses(job.form_id)
    result = SyncResult(fetched=len(responses))

    existing = {
        c.response_id: c
        for c in session.scalars(select(Candidate).where(Candidate.job_id == job.id)).all()
    }

    for response in responses:
        response_id = response.get("responseId")
        if not response_id:
            continue

        fields, unmapped = response_to_fields(response, job.question_map)
        result.unmapped_answers += unmapped

        candidate = existing.get(response_id)
        if candidate is None:
            candidate = Candidate(job_id=job.id, response_id=response_id)
            session.add(candidate)
            result.created += 1
        else:
            result.updated += 1

        for key, value in fields.items():
            setattr(candidate, key, value)

        candidate.raw_response = response
        candidate.submitted_at = forms_api.parse_timestamp(
            response.get("lastSubmittedTime") or response.get("createTime")
        )
        candidate.status = CandidateStatus.new.value

    session.commit()
    return result


def screen_candidate(
    session: Session, job: JobOpening, candidate: Candidate, *, use_llm: bool = True
) -> Candidate:
    """Run both filter stages on one candidate and persist the verdict."""
    outcome = apply_rules(candidate, job)

    candidate.missing_fields = outcome.missing_fields
    candidate.rule_failures = outcome.rule_failures
    candidate.screened_at = dt.datetime.now(dt.timezone.utc)

    if not outcome.is_complete:
        candidate.status = CandidateStatus.rejected_incomplete.value
        candidate.rationale = "Required fields were left blank: " + ", ".join(outcome.missing_fields)
        candidate.fit_score = None
        candidate.recommendation = None
        candidate.assessment = {}
        session.commit()
        return candidate

    if not outcome.passes_rules:
        candidate.status = CandidateStatus.rejected_rules.value
        candidate.rationale = " ".join(outcome.rule_failures)
        candidate.fit_score = None
        candidate.recommendation = None
        candidate.assessment = {}
        session.commit()
        return candidate

    if not use_llm:
        candidate.status = CandidateStatus.shortlisted.value
        candidate.rationale = (
            "Passed all completeness and hard-requirement checks (AI scoring skipped)."
        )
        session.commit()
        return candidate

    assessment = score_candidate(job, candidate, outcome)
    cutoff = float(job.thresholds["score_cutoff"])

    candidate.fit_score = max(0, min(100, assessment.fit_score))
    candidate.recommendation = assessment.recommendation

    dump = assessment.model_dump()
    dump["model_recommendation"] = assessment._raw_recommendation
    if assessment._raw_fit_score is not None:
        dump["model_fit_score"] = assessment._raw_fit_score
    candidate.assessment = dump
    candidate.rationale = assessment.rationale
    candidate.status = (
        CandidateStatus.shortlisted.value
        if candidate.fit_score >= cutoff
        else CandidateStatus.rejected_score.value
    )
    session.commit()
    return candidate


def screen_job(
    session: Session, job: JobOpening, *, use_llm: bool = True, rescreen: bool = False
) -> ScreenResult:
    """Screen every unscreened candidate on ``job`` (or all of them with ``rescreen``)."""
    query = select(Candidate).where(Candidate.job_id == job.id)
    if not rescreen:
        query = query.where(Candidate.status == CandidateStatus.new.value)

    candidates = session.scalars(query).all()
    result = ScreenResult(total=len(candidates))

    for candidate in candidates:
        try:
            screen_candidate(session, job, candidate, use_llm=use_llm)
        except ScoringRefusal as exc:
            session.rollback()
            result.errors.append(f"candidate {candidate.id}: {exc}")
            logger.warning("Scoring refused for candidate %s: %s", candidate.id, exc)
            continue
        except ScoringError as exc:
            session.rollback()
            result.errors.append(f"candidate {candidate.id}: {exc}")
            logger.warning("Scoring failed for candidate %s: %s", candidate.id, exc)
            continue

        match candidate.status:
            case CandidateStatus.shortlisted.value:
                result.shortlisted += 1
            case CandidateStatus.rejected_incomplete.value:
                result.rejected_incomplete += 1
            case CandidateStatus.rejected_rules.value:
                result.rejected_rules += 1
            case CandidateStatus.rejected_score.value:
                result.rejected_score += 1

    return result
