"""Deterministic filtering — runs before the model ever sees a candidate.

Two gates, in order:

1. **Completeness.** Any field the admin marked required that came back blank (or
   syntactically invalid, for email/phone/URL) rejects the response outright.
2. **Hard rules.** Experience floor, must-have skill coverage, notice period, and
   compensation ceiling — each scaled by the admin's strictness setting.

Everything here is explainable: a rejection always carries the exact reasons, so an
admin can audit or override it. Only survivors cost an API call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.field_map import FIELDS_BY_KEY, LIST_FIELDS
from app.models import Candidate, JobOpening
from app.parsing import looks_like_email, looks_like_phone, looks_like_url

#: canonical field -> column-level validator for syntactically broken answers
_VALIDATORS = {
    "email": looks_like_email,
    "phone": looks_like_phone,
    "resume_url": looks_like_url,
    "linkedin": looks_like_url,
    "portfolio_url": looks_like_url,
}


@dataclass
class RuleOutcome:
    missing_fields: list[str] = field(default_factory=list)
    rule_failures: list[str] = field(default_factory=list)
    matched_required_skills: list[str] = field(default_factory=list)
    missing_required_skills: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields

    @property
    def passes_rules(self) -> bool:
        return not self.rule_failures


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9+#.]+", " ", text.lower()).strip()


def _value_for(candidate: Candidate, key: str) -> object:
    return getattr(candidate, key, None)


def check_completeness(candidate: Candidate, job: JobOpening) -> list[str]:
    """Return the human-readable labels of required fields that are blank or malformed."""
    missing: list[str] = []
    for key in job.required_fields or []:
        spec = FIELDS_BY_KEY.get(key)
        label = spec.title if spec else key
        value = _value_for(candidate, key)

        if key in LIST_FIELDS:
            if not value:
                missing.append(label)
            continue

        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(label)
            continue

        validator = _VALIDATORS.get(key)
        if validator and isinstance(value, str) and not validator(value):
            missing.append(f"{label} (invalid format)")

    return missing


def _candidate_skill_haystack(candidate: Candidate) -> str:
    """Skills are often mentioned outside the skills question — search the whole profile."""
    chunks = [
        " ".join(candidate.skills or []),
        candidate.current_role or "",
        candidate.cover_note or "",
        candidate.education or "",
    ]
    return _normalise(" ".join(chunks))


def match_required_skills(candidate: Candidate, job: JobOpening) -> tuple[list[str], list[str]]:
    """Split ``job.required_skills`` into (matched, missing) for this candidate."""
    haystack = _candidate_skill_haystack(candidate)
    matched: list[str] = []
    missing: list[str] = []

    for skill in job.required_skills or []:
        needle = _normalise(skill)
        if needle and re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack):
            matched.append(skill)
        else:
            missing.append(skill)

    return matched, missing


def apply_rules(candidate: Candidate, job: JobOpening) -> RuleOutcome:
    outcome = RuleOutcome()
    thresholds = job.thresholds
    slack = float(thresholds["experience_slack"])

    outcome.missing_fields = check_completeness(candidate, job)

    # --- experience floor -------------------------------------------------------------
    floor = job.min_years_experience - slack
    if job.min_years_experience and candidate.years_experience is not None:
        if candidate.years_experience < floor:
            outcome.rule_failures.append(
                f"{candidate.years_experience:g} years of experience is below the "
                f"{job.min_years_experience:g}-year minimum "
                f"(tolerance {slack:g} years)."
            )

    # --- experience ceiling (soft: only bites at the strict end) -----------------------
    if job.max_years_experience is not None and candidate.years_experience is not None:
        ceiling = job.max_years_experience + slack
        if candidate.years_experience > ceiling:
            outcome.rule_failures.append(
                f"{candidate.years_experience:g} years of experience exceeds the "
                f"{job.max_years_experience:g}-year ceiling for this role."
            )

    # --- must-have skills -------------------------------------------------------------
    matched, missing_skills = match_required_skills(candidate, job)
    outcome.matched_required_skills = matched
    outcome.missing_required_skills = missing_skills

    if job.required_skills:
        ratio = len(matched) / len(job.required_skills)
        needed = float(thresholds["must_have_ratio"])
        if ratio < needed:
            outcome.rule_failures.append(
                f"Matched {len(matched)}/{len(job.required_skills)} must-have skills "
                f"({ratio:.0%}), below the {needed:.0%} bar for '{job.strictness}' screening. "
                f"Missing: {', '.join(missing_skills)}."
            )

    # --- notice period ----------------------------------------------------------------
    if job.max_notice_period_days is not None and candidate.notice_period_days is not None:
        if candidate.notice_period_days > job.max_notice_period_days:
            outcome.rule_failures.append(
                f"Notice period of {candidate.notice_period_days} days exceeds the "
                f"{job.max_notice_period_days}-day maximum."
            )

    # --- evidence links ---------------------------------------------------------------
    # At the strict end we refuse to shortlist someone with nothing verifiable attached.
    if not thresholds["allow_missing_optional"]:
        evidence = [candidate.resume_url, candidate.linkedin, candidate.portfolio_url]
        if not any(evidence):
            outcome.rule_failures.append(
                "No resume, LinkedIn, or portfolio link supplied — nothing to verify claims "
                f"against, which '{job.strictness}' screening requires."
            )

    # --- compensation -----------------------------------------------------------------
    if job.max_expected_ctc is not None and candidate.expected_ctc is not None:
        if candidate.expected_ctc > job.max_expected_ctc:
            outcome.rule_failures.append(
                f"Expected compensation of {candidate.expected_ctc:,.0f} is above the "
                f"budgeted ceiling of {job.max_expected_ctc:,.0f}."
            )

    return outcome
