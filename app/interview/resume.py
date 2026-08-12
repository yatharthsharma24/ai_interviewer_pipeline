"""Building the candidate dossier the interviewer works from.

Two sources, in order of trust:

1. The **structured profile** from their application — skills, experience, current role,
   education, and their own words on why they fit. Always present.
2. The **resume document**, when its URL can actually be fetched. Often it cannot: most
   candidates paste a Google Drive share link, and our OAuth scope (``drive.file``) only
   covers files this app created. A failed fetch is reported, never silently ignored,
   because "the resume didn't load" and "the resume was empty" must not look the same.
"""

from __future__ import annotations

import io
import re

import httpx

from app.models import Candidate, JobOpening

_DRIVE_FILE_RE = re.compile(r"drive\.google\.com/file/d/([A-Za-z0-9_-]+)")
_DRIVE_OPEN_RE = re.compile(r"drive\.google\.com/open\?id=([A-Za-z0-9_-]+)")
_DOCS_RE = re.compile(r"docs\.google\.com/document/d/([A-Za-z0-9_-]+)")

MAX_RESUME_CHARS = 12_000


class ResumeFetchError(RuntimeError):
    pass


def direct_download_url(url: str) -> str:
    """Rewrite common share links to something fetchable. Other URLs pass through."""
    match = _DRIVE_FILE_RE.search(url) or _DRIVE_OPEN_RE.search(url)
    if match:
        return f"https://drive.google.com/uc?export=download&id={match.group(1)}"
    match = _DOCS_RE.search(url)
    if match:
        return f"https://docs.google.com/document/d/{match.group(1)}/export?format=txt"
    return url


def _pdf_to_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise ResumeFetchError("pypdf is not installed, so PDF resumes cannot be read.") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise ResumeFetchError(f"Could not read the PDF: {exc}") from exc


def fetch_resume_text(url: str | None, *, timeout: float = 20.0) -> str:
    """Best-effort extraction of resume text. Raises with a usable reason on failure."""
    if not url or not url.strip():
        raise ResumeFetchError("No resume URL on file.")

    target = direct_download_url(url.strip())
    try:
        response = httpx.get(target, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise ResumeFetchError(f"Could not reach {target}: {exc}") from exc

    if response.status_code >= 400:
        raise ResumeFetchError(
            f"{target} returned {response.status_code}. If this is a Google Drive link, the "
            "file is probably not shared publicly — this app can only read files it created."
        )

    content_type = response.headers.get("content-type", "").lower()
    body = response.content

    if "pdf" in content_type or body[:4] == b"%PDF":
        text = _pdf_to_text(body)
    elif "html" in content_type:
        raise ResumeFetchError(
            "The link returned a web page rather than a document, which usually means it is "
            "not publicly shared."
        )
    else:
        text = body.decode("utf-8", errors="replace")

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ResumeFetchError("The document downloaded but contained no extractable text.")
    return text[:MAX_RESUME_CHARS]


def build_dossier(candidate: Candidate, job: JobOpening) -> dict:
    """The identity-bound snapshot stored on the interview.

    Includes ``candidate_id``, name, and email so a stored interview can always be proved
    to belong to the right person.
    """
    assessment = candidate.assessment or {}
    return {
        "candidate_id": candidate.id,
        "full_name": candidate.full_name,
        "email": candidate.email,
        "phone": candidate.phone,
        "years_experience": candidate.years_experience,
        "current_role": candidate.current_role,
        "current_company": candidate.current_company,
        "skills": candidate.skills or [],
        "education": candidate.education,
        "location": candidate.location,
        "linkedin": candidate.linkedin,
        "resume_url": candidate.resume_url,
        "portfolio_url": candidate.portfolio_url,
        "cover_note": candidate.cover_note,
        "screening": {
            "fit_score": candidate.fit_score,
            "recommendation": candidate.recommendation,
            "matched_required_skills": assessment.get("matched_required_skills", []),
            "missing_required_skills": assessment.get("missing_required_skills", []),
            "strengths": assessment.get("strengths", []),
            "concerns": assessment.get("concerns", []),
            "seniority_assessment": assessment.get("seniority_assessment"),
        },
        "job": {
            "title": job.title,
            "department": job.department,
            "required_skills": job.required_skills or [],
            "preferred_skills": job.preferred_skills or [],
            "min_years_experience": job.min_years_experience,
            "description": job.description,
            "responsibilities": job.responsibilities or [],
        },
    }


def dossier_text(dossier: dict, resume_text: str | None = None) -> str:
    """Render the dossier for the interviewer's system prompt."""

    def line(label: str, value: object) -> str | None:
        if value in (None, "", [], {}):
            return None
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        return f"{label}: {value}"

    job = dossier.get("job", {})
    screening = dossier.get("screening", {})

    parts = [
        "ROLE",
        line("Title", job.get("title")),
        line("Team", job.get("department")),
        line("Must-have skills", job.get("required_skills")),
        line("Nice-to-have skills", job.get("preferred_skills")),
        line("Minimum experience (years)", job.get("min_years_experience")),
        line("Description", job.get("description")),
        line("Responsibilities", job.get("responsibilities")),
        "",
        "CANDIDATE",
        line("Name", dossier.get("full_name")),
        line("Years of experience", dossier.get("years_experience")),
        line("Current role", dossier.get("current_role")),
        line("Current company", dossier.get("current_company")),
        line("Skills claimed", dossier.get("skills")),
        line("Education", dossier.get("education")),
        line("In their own words", dossier.get("cover_note")),
        "",
        "SCREENING NOTES (from the automated review — not shared with the candidate)",
        line("Fit score", screening.get("fit_score")),
        line("Skills evidenced", screening.get("matched_required_skills")),
        line("Skills with no evidence", screening.get("missing_required_skills")),
        line("Strengths", screening.get("strengths")),
        line("Things to probe", screening.get("concerns")),
    ]

    if resume_text:
        parts += ["", "RESUME TEXT", resume_text]

    return "\n".join(p for p in parts if p is not None)
