"""Building the interview's question plan.

Generated once, before the call, rather than improvised live. Three reasons: the plan can be
reviewed before a candidate ever sees it, the realtime model spends its budget on
conversation instead of planning, and two candidates for the same role get comparably
structured interviews.

The plan is a spine, not a script — the interviewer follows up freely within each topic.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.interview.models import DIFFICULTY_PROFILES, Difficulty
from app.interview.providers import complete_json
from app.interview.resume import dossier_text

Topic = Literal["experience", "technical", "resume_probe", "role_fit", "behavioural"]


class PlannedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: Topic = Field(description="Which area of the interview this question belongs to.")
    question: str = Field(description="The question, phrased exactly as it should be asked aloud.")
    why: str = Field(description="One sentence on what a good answer would demonstrate.")
    follow_up: str = Field(description="One follow-up to use if the first answer is vague.")


class QuestionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opening: str = Field(description="How the interviewer opens the call, in one or two sentences.")
    questions: list[PlannedQuestion] = Field(description="The planned questions, in order.")
    closing: str = Field(description="How the interviewer closes the call.")


PLANNER_SYSTEM = """\
You design interview plans for a hiring team. You are given a role, a candidate's \
application, and optionally their resume text. You return a structured plan of questions \
the interviewer will ask aloud in a live voice call.

Rules:
- Ground questions in this candidate's actual background. "You mentioned sharding the \
billing database — walk me through that decision" beats "Tell me about databases".
- Cover the role's must-have skills. Where the screening notes flagged a skill as \
unevidenced, that is exactly what to probe.
- Mix topics: their real experience, technical depth, direct probes on resume claims, and \
fit for this specific role.
- Questions are spoken aloud, so keep each to one or two sentences. No multi-part questions \
and no written exercises.
- Never ask about age, marital status, religion, nationality, health, or anything else \
unrelated to the ability to do this job."""


def build_question_plan(
    dossier: dict, difficulty: str, resume_text: str | None = None
) -> tuple[QuestionPlan, str]:
    """Ask the planning model for a structured plan for this candidate.

    Returns ``(plan, provider)`` — the caller records which provider produced it, so a
    silent failover to Gemini is still visible afterwards.
    """
    profile = DIFFICULTY_PROFILES[Difficulty(difficulty)]

    user = (
        f"{dossier_text(dossier, resume_text)}\n\n"
        f"INTERVIEW DIFFICULTY: {difficulty}\n"
        f"{profile['guidance']}\n\n"
        f"Produce exactly {profile['questions']} questions."
    )

    parsed, provider = complete_json(
        PLANNER_SYSTEM,
        user,
        schema_name="question_plan",
        schema=QuestionPlan.model_json_schema(),
    )
    return QuestionPlan.model_validate(parsed), provider


def fallback_plan(dossier: dict, difficulty: str) -> QuestionPlan:
    """A usable plan built without an API call.

    The interview must not be impossible to run because the planner was unreachable, so this
    derives questions directly from the job's required skills and the screening concerns.
    """
    job = dossier.get("job", {})
    screening = dossier.get("screening", {})
    name = (dossier.get("full_name") or "there").split()[0]
    count = int(DIFFICULTY_PROFILES[Difficulty(difficulty)]["questions"])

    questions: list[PlannedQuestion] = [
        PlannedQuestion(
            topic="experience",
            question=(
                f"Tell me about the work you did as {dossier.get('current_role')} "
                f"at {dossier.get('current_company')}."
                if dossier.get("current_role")
                else "Tell me about the most substantial project you have worked on."
            ),
            why="Establishes what they have actually built.",
            follow_up="What part of that was yours specifically?",
        )
    ]

    for skill in (job.get("required_skills") or [])[: count - 2]:
        questions.append(
            PlannedQuestion(
                topic="technical",
                question=f"Walk me through something non-trivial you have built with {skill}.",
                why=f"Tests real depth in {skill}, a must-have for this role.",
                follow_up=f"What went wrong, and how did you diagnose it?",
            )
        )

    for concern in (screening.get("concerns") or [])[:2]:
        questions.append(
            PlannedQuestion(
                topic="resume_probe",
                question=f"I want to understand this better: {concern}. Can you talk me through it?",
                why="Directly addresses a gap flagged during screening.",
                follow_up="Can you give me a concrete example?",
            )
        )

    questions.append(
        PlannedQuestion(
            topic="role_fit",
            question=f"What attracted you to this {job.get('title', 'role')}?",
            why="Checks genuine interest in this role rather than any role.",
            follow_up="What would make you turn it down?",
        )
    )

    return QuestionPlan(
        opening=(
            f"Hi {name}, thanks for joining. I'm the AI interviewer for the "
            f"{job.get('title', 'role')} position. This will take about 20 minutes and I'll "
            "ask about your background and some technical detail. Ready to start?"
        ),
        questions=questions[:count],
        closing=(
            "That's everything I wanted to cover. Thanks for your time — the team will be "
            "in touch about next steps. Goodbye."
        ),
    )
