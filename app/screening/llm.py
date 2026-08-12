"""Fit scoring for candidates that survived the deterministic filters.

Provider-agnostic: this module owns the schema, the prompt, and the parsing; the actual
call goes through a backend (Ollama or OpenAI) from ``app.screening.backends``.

Prompt layout matters. The job spec is the **stable** half and goes in the system message;
the candidate profile is the volatile half and goes last. That ordering lets a hosted
provider reuse a cached prefix, and lets Ollama keep the model warm across a batch. Don't
reorder them.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from app.models import Candidate, JobOpening
from app.screening.backends import (
    BackendError,
    BackendRefusal,
    get_backend,
)
from app.screening.rules import RuleOutcome

Recommendation = Literal["strong_yes", "yes", "maybe", "no"]

UNSUPPORTED_EVIDENCE_CAP = 49


class FitAssessment(BaseModel):
    """Schema the model is constrained to. Every field is required — no defaults.

    Kept flat and free of numeric/length constraints on purpose: OpenAI's strict mode
    rejects those keywords, and Ollama's grammar-constrained decoding handles a flat
    schema far more reliably than a nested one on small local models.
    """

    model_config = ConfigDict(extra="forbid")

    fit_score: int = Field(description="Overall fit for this specific role, 0-100.")
    describes_concrete_work: bool = Field(
        description=(
            "A factual question about the text, not a quality judgement. "
            "True only if the application names at least one specific thing this candidate "
            "personally built, shipped, decided, fixed, migrated, or measured — a named "
            "system, a technical decision, a problem solved, or a number. "
            "False if it only lists technologies, job titles, and generic self-description "
            "such as 'hardworking' or 'passionate about technology'."
        )
    )
    recommendation: Recommendation = Field(
        description="strong_yes = interview now, yes = interview, maybe = borderline, no = reject."
    )
    matched_required_skills: list[str] = Field(
        description="Required skills with credible evidence in the application."
    )
    missing_required_skills: list[str] = Field(
        description="Required skills with no credible evidence in the application."
    )
    strengths: list[str] = Field(description="Concrete reasons this candidate is a good fit.")
    concerns: list[str] = Field(description="Concrete risks or gaps a recruiter should probe.")
    seniority_assessment: str = Field(
        description="One sentence on whether their depth matches the level of the role."
    )
    rationale: str = Field(
        description="Two to four sentences justifying the score, citing the application text."
    )

    _raw_recommendation: str | None = PrivateAttr(default=None)
    _raw_fit_score: int | None = PrivateAttr(default=None)


SYSTEM_PREAMBLE = """\
You are screening job applications for a hiring team. You are given a role specification \
and one candidate's application, and you return a structured assessment as JSON.

How to score:
- Judge the candidate only against this specific role, not against a general bar.
- Weight demonstrated, specific evidence over self-declared claims. "Built and shipped a \
Django payments service" is stronger evidence than ticking "Python" on a checkbox.
- Missing information is not a negative in itself; say so in `concerns` rather than \
inventing a deficiency. Never assume facts the application does not contain.
- Depth and relevance of experience matter more than raw years.

Unsupported claims are weak evidence, not strong evidence. A list of technologies with no \
description of what the candidate built with them tells you almost nothing, and generic \
self-description ("hardworking", "passionate about technology", "team player") tells you \
nothing at all. Before rewarding a skill, look for what the candidate actually did with it: \
a system, a decision they made, a problem they solved, a number.

Score bands, follow these exactly:
- 85-100: exceptional, clearly above the bar for this role.
- 70-84: solid, would interview.
- 50-69: borderline, real gaps.
- 25-49: weak fit.
- 0-24: not relevant to this role at all.

A candidate missing most of the must-have skills scores below 40 no matter how many years \
of experience they have, because those years are in a different stack.

Answer `describes_concrete_work` honestly and independently of the score. It is a question \
about what the application says, not about whether the candidate matches the role.

Be calibrated: most real applicant pools are mediocre, and inflating scores makes the \
shortlist useless. Report what the evidence supports, including when that is unflattering."""


class ScoringError(BackendError):
    pass


class ScoringRefusal(BackendRefusal):
    pass


def _output_schema() -> dict:
    return FitAssessment.model_json_schema()


def recommendation_for_score(score: int) -> Recommendation:
    """The score bands from the prompt, as code.

    Small local models routinely return a label that contradicts their own number — in
    benchmarking, two of five returned ``strong_yes`` alongside a score of 25. ``fit_score``
    is what the shortlist cutoff actually compares against, so the label is derived from it
    rather than trusted. The model's original answer is kept on ``_raw_recommendation``: a
    wide disagreement is a useful signal that the assessment is low-confidence.
    """
    if score >= 85:
        return "strong_yes"
    if score >= 70:
        return "yes"
    if score >= 50:
        return "maybe"
    return "no"


def build_prompt(job: JobOpening, candidate: Candidate, rules: RuleOutcome) -> tuple[str, str]:
    """Return ``(system, user)``. Stable content in system, volatile content in user."""
    system = (
        f"{SYSTEM_PREAMBLE}\n\n"
        f"<role_specification>\n{job.spec_text()}\n</role_specification>"
    )

    prefilter = []
    if rules.matched_required_skills:
        prefilter.append("Keyword match found for: " + ", ".join(rules.matched_required_skills))
    if rules.missing_required_skills:
        prefilter.append(
            "No keyword match for: "
            + ", ".join(rules.missing_required_skills)
            + " (a keyword miss is not proof of absence — check the free-text answers)."
        )
    if rules.missing_fields:
        prefilter.append("Fields the candidate left blank: " + ", ".join(rules.missing_fields))

    user = (
        "<candidate_application>\n"
        f"{candidate.profile_text()}\n"
        "</candidate_application>\n\n"
    )
    if prefilter:
        user += (
            "<automated_prefilter_notes>\n"
            + "\n".join(prefilter)
            + "\n</automated_prefilter_notes>\n\n"
        )
    user += "Assess this candidate against the role specification. Respond with JSON only."

    return system, user


def score_candidate(job: JobOpening, candidate: Candidate, rules: RuleOutcome) -> FitAssessment:
    """Return the model's structured fit assessment for one candidate."""
    backend = get_backend()
    system, user = build_prompt(job, candidate, rules)

    try:
        content = backend.complete_json(system, user, _output_schema())
    except BackendRefusal as exc:
        raise ScoringRefusal(str(exc)) from exc
    except BackendError as exc:
        raise ScoringError(str(exc)) from exc

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ScoringError(
            f"{backend.describe()} returned text that is not JSON: {content[:200]!r}"
        ) from exc

    try:
        assessment = FitAssessment.model_validate(data)
    except ValueError as exc:
        raise ScoringError(
            f"{backend.describe()} returned JSON that does not match the schema: {exc}"
        ) from exc

    assessment.fit_score = max(0, min(100, assessment.fit_score))
    _apply_evidence_cap(assessment)

    assessment._raw_recommendation = assessment.recommendation
    assessment.recommendation = recommendation_for_score(assessment.fit_score)

    return assessment


def _apply_evidence_cap(assessment: FitAssessment) -> None:
    """Enforce the unsupported-claims ceiling in code rather than in the prompt.

    Benchmarking showed every local model reliably *notices* a keyword-stuffed application
    — the rationale says so in plain words — while still scoring it 65-85. Two attempts to
    fix that in the prompt failed: a "score no higher than 49" instruction was ignored, and
    a three-way ``specific``/``partial``/``unsupported`` grade collapsed to ``partial`` as a
    comfortable middle. What works is asking a *factual* yes/no question about the text and
    doing the arithmetic here. The pre-cap score is kept on ``_raw_fit_score`` so the
    adjustment stays auditable.
    """
    if assessment.describes_concrete_work:
        return
    if assessment.fit_score <= UNSUPPORTED_EVIDENCE_CAP:
        return

    assessment._raw_fit_score = assessment.fit_score
    assessment.fit_score = UNSUPPORTED_EVIDENCE_CAP
    note = (
        "Score capped: the application lists skills but does not describe what the candidate "
        f"built with them (model scored {assessment._raw_fit_score} on unsupported claims)."
    )
    if note not in assessment.concerns:
        assessment.concerns.append(note)
