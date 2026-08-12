"""Seed a realistic job with candidates so the dashboard has something to show.

Writes to your real database. Everything it creates hangs off one job, so deleting that job
from the dashboard's danger zone removes all of it.

    python scripts/demo_seed.py          # create
    python scripts/demo_seed.py --clean  # remove anything this script made
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.interview.db import init_interview_db
from app.models import Candidate, CandidateStatus, JobOpening

DEMO_TITLE = "Senior Backend Engineer (demo)"

PEOPLE = [
    ("Asha Rao", 92, 7.0, ["Python", "Django", "PostgreSQL", "Docker"],
     "I own PayFlow's billing service: Django and PostgreSQL at ~4k requests a second. I led "
     "the sharding migration that cut p99 latency from 800ms to 120ms.",
     CandidateStatus.shortlisted),
    ("Priya Nair", 84, 6.0, ["Python", "Flask", "PostgreSQL", "Docker"],
     "Six years of Python services, mostly Flask. I ran the schema migration for a 300M-row "
     "orders table and cut our nightly batch from 6 hours to 40 minutes.",
     CandidateStatus.shortlisted),
    ("Ravi Kumar", 75, 4.5, ["Python", "Django"],
     "Four years building Django APIs for an e-commerce catalogue. I have used MySQL rather "
     "than PostgreSQL, but the SQL work is similar.",
     CandidateStatus.shortlisted),
    ("Sam Iyer", 49, 5.0, ["Python", "Django", "PostgreSQL", "Docker", "AWS", "Kubernetes"],
     "Python Django PostgreSQL Docker AWS Kubernetes React Node microservices agile scrum. "
     "Hardworking team player passionate about technology.",
     CandidateStatus.rejected_score),
    ("Neha Singh", 35, 1.0, ["Python", "Django", "PostgreSQL"],
     "I built two internal CRUD dashboards with Django in my first year.",
     CandidateStatus.rejected_score),
    ("Vikram Patel", None, 9.0, ["Java", "Spring Boot", "Oracle"],
     "Nine years of enterprise Java and Spring Boot on Oracle. I have not used Python "
     "professionally.",
     CandidateStatus.rejected_rules),
    ("Meera Joshi", None, 3.0, ["Python", "Django"],
     "Backend developer looking for a new challenge.",
     CandidateStatus.rejected_incomplete),
]

ASSESSMENTS = {
    "Asha Rao": {
        "strengths": ["Owns a high-throughput billing service end to end",
                      "Measurable impact: p99 800ms to 120ms"],
        "concerns": ["No evidence of leading or mentoring a team"],
        "matched_required_skills": ["Python", "Django", "PostgreSQL"],
        "missing_required_skills": [],
    },
    "Priya Nair": {
        "strengths": ["Deep PostgreSQL work at real scale", "Quantified batch improvement"],
        "concerns": ["Flask rather than Django in production — Django depth unproven"],
        "matched_required_skills": ["Python", "PostgreSQL"],
        "missing_required_skills": ["Django"],
    },
    "Ravi Kumar": {
        "strengths": ["Solid Django API experience"],
        "concerns": ["MySQL not PostgreSQL", "No evidence of scale beyond a catalogue"],
        "matched_required_skills": ["Python", "Django"],
        "missing_required_skills": ["PostgreSQL"],
    },
    "Sam Iyer": {
        "strengths": ["Lists every required technology"],
        "concerns": ["Score capped: the application lists skills but does not describe what "
                     "the candidate built with them"],
        "matched_required_skills": ["Python", "Django", "PostgreSQL"],
        "missing_required_skills": [],
        "model_fit_score": 75,
        "model_recommendation": "yes",
    },
    "Neha Singh": {
        "strengths": ["Right stack for the role"],
        "concerns": ["One year of experience against a four-year floor",
                     "Internal CRUD work only"],
        "matched_required_skills": ["Python", "Django", "PostgreSQL"],
        "missing_required_skills": [],
    },
}


def clean(session) -> int:
    jobs = session.scalars(select(JobOpening).where(JobOpening.title == DEMO_TITLE)).all()
    for job in jobs:
        session.delete(job)
    session.commit()
    return len(jobs)


def main() -> int:
    init_db()
    init_interview_db()

    with SessionLocal() as session:
        removed = clean(session)
        if "--clean" in sys.argv:
            print(f"Removed {removed} demo job(s) and everything attached to them.")
            return 0

        job = JobOpening(
            title=DEMO_TITLE,
            department="Platform",
            location="Bengaluru",
            employment_type="full-time",
            description="Own and scale our payments and billing services.",
            responsibilities=["Design and own REST APIs", "Own service reliability",
                              "Mentor engineers"],
            min_years_experience=4,
            required_skills=["Python", "Django", "PostgreSQL"],
            preferred_skills=["Docker", "AWS"],
            max_notice_period_days=60,
            strictness="balanced",
            required_fields=["full_name", "email", "phone", "years_experience", "skills",
                             "resume_url"],
            status="open",
        )
        session.add(job)
        session.commit()

        for index, (name, score, years, skills, note, status) in enumerate(PEOPLE):
            first = name.split()[0].lower()
            missing = ["Mobile number"] if status is CandidateStatus.rejected_incomplete else []
            session.add(
                Candidate(
                    job_id=job.id,
                    response_id=f"demo-{index}",
                    full_name=name,
                    email=f"{first}@example.com",
                    phone=None if missing else f"+9198765{43210 + index}",
                    years_experience=years,
                    current_role="Backend Engineer",
                    current_company=["PayFlow", "LogiTech", "ShopCo", "Consultancy",
                                     "StartupX", "BigCorp", "Unknown"][index],
                    skills=skills,
                    education="B.Tech Computer Science",
                    location="Bengaluru",
                    notice_period_days=30,
                    resume_url="https://example.com/cv.pdf",
                    cover_note=note,
                    fit_score=score,
                    recommendation=(
                        "strong_yes" if (score or 0) >= 85
                        else "yes" if (score or 0) >= 70
                        else "maybe" if (score or 0) >= 50
                        else "no" if score is not None else None
                    ),
                    status=status.value,
                    missing_fields=missing,
                    rule_failures=(
                        ["Matched 0/3 must-have skills (0%) — those years are in a different stack."]
                        if status is CandidateStatus.rejected_rules else []
                    ),
                    rationale=(
                        "Required fields were left blank: Mobile number"
                        if missing else
                        "Deep, specific experience on exactly this stack with measurable impact."
                        if (score or 0) >= 85 else
                        "Relevant experience with a real gap against one must-have skill."
                        if (score or 0) >= 70 else
                        "Lists the right technologies but never describes what was built with them."
                        if name == "Sam Iyer" else
                        "Well short of the experience bar for a senior role."
                        if score is not None else
                        "Nine years in a different stack; none of the must-have skills evidenced."
                    ),
                    assessment=ASSESSMENTS.get(name, {}),
                )
            )
        session.commit()
        job_id = job.id

    print(f"Seeded job {job_id}: {DEMO_TITLE}")
    print(f"  {len(PEOPLE)} candidates across every funnel state")
    print("\nOpen the dashboard:  python -m app.cli serve   ->  http://127.0.0.1:8000/admin")
    print("Remove it again:     python scripts/demo_seed.py --clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
