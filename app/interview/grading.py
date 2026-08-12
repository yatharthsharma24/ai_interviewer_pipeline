"""Post-interview summary and ratings.

Runs after the call, on the stored transcript, using a normal text model rather than the
realtime one — grading wants deliberation, not latency.

The realtime interviewer is deliberately kept out of this. An interviewer that is also the
grader tends to score its own conversation kindly, and the candidate can hear evaluation
leaking into the questions. Separating them also means grading can be re-run, tuned, or
audited without touching a recording.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.interview.models import Interview
from app.interview.providers import complete_json

Verdict = Literal["strong_hire", "hire", "borderline", "no_hire"]
Level = Literal["excellent", "good", "adequate", "weak", "not_demonstrated"]


class CompetencyRating(BaseModel):
    model_config = ConfigDict(extra="forbid")

    competency: str = Field(description="What is being rated, e.g. 'Python depth'.")
    level: Level = Field(description="How well the candidate demonstrated it in this interview.")
    score: int = Field(description="0-100 for this competency alone.")
    evidence: str = Field(
        description="What they actually said that supports this, quoted or closely paraphrased."
    )


class AnswerNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(description="The question as asked.")
    answer_quality: Level = Field(description="How well they answered this specific question.")
    note: str = Field(description="One sentence on what the answer showed.")


class InterviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overall_rating: int = Field(description="Overall interview performance, 0-100.")
    recommendation: Verdict = Field(description="The hiring recommendation from this interview.")
    summary: str = Field(
        description="Three to five sentences a hiring manager can read on its own."
    )
    competencies: list[CompetencyRating] = Field(description="Per-competency breakdown.")
    per_question: list[AnswerNote] = Field(description="One note per question actually asked.")
    strengths: list[str] = Field(description="What this candidate demonstrably did well.")
    concerns: list[str] = Field(description="Gaps or risks the transcript actually shows.")
    red_flags: list[str] = Field(
        description="Serious issues: dishonesty, contradictions with the resume, hostility. "
        "Empty list if none — do not invent one."
    )
    communication: str = Field(description="One sentence on clarity and structure of speech.")
    interview_completed: bool = Field(
        description="True if the interview covered its questions and ended normally."
    )


GRADER_SYSTEM = """\
You assess a completed job interview from its transcript. You did not conduct it.

How to assess:
- Judge only what the transcript shows. If a competency never came up, mark it \
`not_demonstrated` rather than guessing from the resume.
- Quote or closely paraphrase what the candidate actually said as evidence. A rating with \
no evidence in the transcript is not a rating.
- Weight demonstrated depth over confidence. A candidate who says "I don't know, but I'd \
find out by..." is stronger than one who bluffs fluently.
- Fluent speech is not competence, and hesitant speech is not incompetence. Rate \
communication separately from technical substance.
- A short transcript means limited evidence: say so and lower confidence, rather than \
inferring a verdict from very little.
- `red_flags` is for serious issues only — dishonesty, direct contradiction of the resume, \
hostility. Ordinary weakness is a concern, not a red flag. An empty list is the normal case.

Scoring: 85-100 exceptional. 70-84 clearly hire. 50-69 borderline. 25-49 weak. 0-24 not \
close. Be calibrated — inflated interview scores make the whole pipeline useless."""


def _integrity_section(interview: Interview) -> str:
    if not interview.events:
        return "No proctoring violations were recorded."
    counted = [e for e in interview.events if e.counted]
    if not counted:
        return "Minor observations only; nothing met the violation threshold."
    lines = ["Proctoring violations recorded during this call:"]
    for event in counted:
        stamp = f"{int(event.at_seconds // 60):02d}:{int(event.at_seconds % 60):02d}"
        lines.append(f"  [{stamp}] {event.kind} for {event.duration_seconds:.1f}s")
    lines.append(
        "Report these factually in `concerns` if relevant. Do NOT treat them as proof of "
        "cheating — a dropped camera or a glance at a second monitor looks identical to "
        "misconduct from here."
    )
    return "\n".join(lines)


def grade_interview(interview: Interview) -> tuple[InterviewReport, str]:
    """Produce the structured report for a completed interview.

    Returns ``(report, provider)`` so the record shows which model actually graded it.
    """
    dossier = interview.resume_snapshot or {}
    job = dossier.get("job", {})
    duration = interview.duration_seconds()

    user = "\n".join(
        [
            f"ROLE: {job.get('title', 'unknown')}",
            f"Must-have skills: {', '.join(job.get('required_skills', [])) or 'none specified'}",
            f"Interview difficulty: {interview.difficulty}",
            f"Call duration: {duration / 60:.1f} minutes" if duration else "Call duration: unknown",
            f"End reason: {interview.end_reason or 'normal'}",
            "",
            _integrity_section(interview),
            "",
            "TRANSCRIPT",
            interview.transcript_text(),
        ]
    )

    parsed, provider = complete_json(
        GRADER_SYSTEM,
        user,
        schema_name="interview_report",
        schema=InterviewReport.model_json_schema(),
    )

    report = InterviewReport.model_validate(parsed)
    report.overall_rating = max(0, min(100, report.overall_rating))
    for competency in report.competencies:
        competency.score = max(0, min(100, competency.score))
    return report, provider
