"""Canonical candidate fields.

One list drives three things:
  1. which questions the agent creates on a generated Google Form,
  2. how answers on *any* form (ours or the admin's) map back to DB columns,
  3. which fields the completeness filter can be told to require.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Literal

QuestionType = Literal["short_text", "paragraph", "number", "checkbox"]


@dataclass(frozen=True)
class FieldSpec:
    key: str
    title: str
    qtype: QuestionType
    description: str = ""
    default_required: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)


FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        key="full_name",
        title="Full name",
        qtype="short_text",
        default_required=True,
        aliases=("full name", "name", "your name", "candidate name", "applicant name"),
    ),
    FieldSpec(
        key="email",
        title="Email address",
        qtype="short_text",
        description="We will use this for all interview correspondence.",
        default_required=True,
        aliases=("email", "email address", "e-mail", "mail id", "contact email"),
    ),
    FieldSpec(
        key="phone",
        title="Mobile number",
        qtype="short_text",
        description="Include country code, e.g. +91 98765 43210",
        default_required=True,
        aliases=("mobile", "phone", "mobile number", "phone number", "contact number", "sms"),
    ),
    FieldSpec(
        key="years_experience",
        title="Total years of professional experience",
        qtype="number",
        description="Enter a number, e.g. 3 or 4.5",
        default_required=True,
        aliases=(
            "years of experience",
            "total experience",
            "experience in years",
            "work experience",
            "yoe",
        ),
    ),
    FieldSpec(
        key="current_role",
        title="Current job title",
        qtype="short_text",
        aliases=("current role", "current job title", "designation", "current position", "job title"),
    ),
    FieldSpec(
        key="current_company",
        title="Current company",
        qtype="short_text",
        aliases=("current company", "employer", "organisation", "organization", "company"),
    ),
    FieldSpec(
        key="skills",
        title="Which of these technologies have you worked with professionally?",
        qtype="checkbox",
        description="Select every one you have shipped production work with.",
        default_required=True,
        aliases=("skills", "technologies", "tech stack", "tools", "technical skills"),
    ),
    FieldSpec(
        key="education",
        title="Highest qualification and field of study",
        qtype="short_text",
        aliases=("education", "qualification", "degree", "highest qualification", "academic"),
    ),
    FieldSpec(
        key="location",
        title="Current city",
        qtype="short_text",
        aliases=("location", "city", "current city", "based in", "place"),
    ),
    FieldSpec(
        key="notice_period_days",
        title="Notice period in days",
        qtype="number",
        description="Enter 0 if you can join immediately.",
        aliases=("notice period", "notice", "availability", "joining time", "days to join"),
    ),
    FieldSpec(
        key="expected_ctc",
        title="Expected annual compensation",
        qtype="number",
        description="Numbers only, in your local currency.",
        aliases=("expected ctc", "expected salary", "compensation", "salary expectation", "ctc"),
    ),
    FieldSpec(
        key="linkedin",
        title="LinkedIn profile URL",
        qtype="short_text",
        aliases=("linkedin", "linked in", "linkedin url", "linkedin profile"),
    ),
    FieldSpec(
        key="resume_url",
        title="Link to your resume",
        qtype="short_text",
        description="A public Google Drive / Dropbox link works. Make sure sharing is enabled.",
        default_required=True,
        aliases=("resume", "cv", "resume link", "resume url", "upload resume", "cv link"),
    ),
    FieldSpec(
        key="portfolio_url",
        title="Portfolio or GitHub link",
        qtype="short_text",
        aliases=("portfolio", "github", "git hub", "work samples", "personal site"),
    ),
    FieldSpec(
        key="cover_note",
        title="Why are you a strong fit for this role?",
        qtype="paragraph",
        description="A few sentences is plenty.",
        aliases=(
            "why are you a fit",
            "cover letter",
            "cover note",
            "tell us about yourself",
            "motivation",
            "why should we hire you",
        ),
    ),
)

FIELDS_BY_KEY: dict[str, FieldSpec] = {f.key: f for f in FIELDS}

DEFAULT_REQUIRED_FIELDS: list[str] = [f.key for f in FIELDS if f.default_required]

LIST_FIELDS = {"skills"}


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def match_question_title(title: str, *, cutoff: float = 0.72) -> str | None:
    """Best-effort map an arbitrary Google Form question title to a canonical field key.

    Used when the admin supplies a form they built by hand. Exact/substring alias hits win;
    otherwise fall back to fuzzy ratio so 'Total yrs of experience' still lands on
    ``years_experience``. Returns ``None`` when nothing is close enough — the answer is still
    preserved in ``Candidate.raw_response``.
    """
    norm = _normalise(title)
    if not norm:
        return None

    best_key: str | None = None
    best_score = 0.0

    for spec in FIELDS:
        for alias in (*spec.aliases, _normalise(spec.title)):
            alias_norm = _normalise(alias)
            if not alias_norm:
                continue
            if norm == alias_norm:
                return spec.key
            if alias_norm in norm or norm in alias_norm:
                score = 0.95
            else:
                score = difflib.SequenceMatcher(None, norm, alias_norm).ratio()
            if score > best_score:
                best_score, best_key = score, spec.key

    return best_key if best_score >= cutoff else None
