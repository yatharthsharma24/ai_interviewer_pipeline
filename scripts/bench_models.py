"""Compare local Ollama models on the real screening prompt.

Screening quality is the whole product, and small local models vary wildly at it.

The metric is **shortlist decision accuracy**: for each candidate, does the model's score
land it on the correct side of the strictness cutoff? That is the only thing the pipeline
does with the number, so it is the only thing worth optimising. Ranking order and "is the
score in a plausible band" are reported too, but a model that ranks candidates perfectly
while putting an unqualified one above the cutoff is worse than one that ranks them
sloppily and still calls every shortlist correctly.

False positives are weighted as worse than false negatives: a bad shortlist wastes
interview time in Part 3, whereas a missed candidate is recoverable by loosening
strictness.

    python scripts/bench_models.py                    # every installed chat model
    python scripts/bench_models.py llama3:latest      # just these

NOTE: six hand-written candidates is a smoke benchmark, not a rigorous eval. Treat the
result as "this model is not obviously broken", and re-run it against your own real
applicant pool once you have one.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.config import get_settings
from app.models import Candidate, JobOpening, Strictness
from app.screening import backends, llm
from app.screening.rules import apply_rules

JOB = JobOpening(
    title="Senior Backend Engineer",
    department="Platform",
    location="Bengaluru",
    employment_type="full-time",
    description="Own and scale our payments and billing services.",
    responsibilities=["Design REST APIs", "Own service reliability", "Mentor engineers"],
    min_years_experience=4,
    required_skills=["Python", "Django", "PostgreSQL"],
    preferred_skills=["Docker", "AWS"],
    strictness=Strictness.balanced.value,
    required_fields=[],
)

CASES: list[tuple[str, tuple[int, int], bool, Candidate]] = [
    (
        "excellent",
        (75, 100),
        True,
        Candidate(
            response_id="c1", full_name="Asha Rao", years_experience=7.0,
            current_role="Senior Backend Engineer", current_company="PayFlow",
            skills=["Python", "Django", "PostgreSQL", "Docker", "AWS"],
            education="B.Tech Computer Science",
            cover_note=(
                "I own PayFlow's billing service: Django + PostgreSQL handling ~4k requests "
                "per second. I led the sharding migration that cut p99 latency from 800ms to "
                "120ms, and I mentor three junior engineers."
            ),
        ),
    ),
    (
        "decent",
        (55, 80),
        True,
        Candidate(
            response_id="c2", full_name="Ravi Kumar", years_experience=4.5,
            current_role="Backend Developer", current_company="ShopCo",
            skills=["Python", "Django"],
            education="B.Sc Information Technology",
            cover_note=(
                "Four years building Django APIs for an e-commerce catalogue. I have used "
                "MySQL rather than PostgreSQL, but the SQL work is similar."
            ),
        ),
    ),
    (
        "junior",
        (25, 60),
        False,
        Candidate(
            response_id="c3", full_name="Neha Singh", years_experience=1.0,
            current_role="Junior Developer", current_company="StartupX",
            skills=["Python", "Django", "PostgreSQL"],
            education="B.E Computer Science",
            cover_note="I built two internal CRUD dashboards with Django in my first year.",
        ),
    ),
    (
        "wrong stack",
        (0, 40),
        False,
        Candidate(
            response_id="c4", full_name="Vikram Patel", years_experience=9.0,
            current_role="Senior Java Engineer", current_company="BigCorp",
            skills=["Java", "Spring Boot", "Oracle"],
            education="M.Tech Software Engineering",
            cover_note=(
                "Nine years of enterprise Java and Spring Boot microservices on Oracle. "
                "I have not used Python professionally."
            ),
        ),
    ),
    (
        "keyword stuffer",
        (20, 55),
        False,
        Candidate(
            response_id="c5", full_name="Sam Iyer", years_experience=5.0,
            current_role="Software Engineer", current_company="Consultancy",
            skills=["Python", "Django", "PostgreSQL", "Docker", "AWS", "Kubernetes"],
            education="B.Tech",
            cover_note=(
                "Python Django PostgreSQL Docker AWS Kubernetes React Node microservices "
                "agile scrum. Hardworking team player passionate about technology."
            ),
        ),
    ),
    (
        "strong but light on one",
        (60, 85),
        True,
        Candidate(
            response_id="c6", full_name="Priya Nair", years_experience=6.0,
            current_role="Backend Engineer", current_company="LogiTech Systems",
            skills=["Python", "Flask", "PostgreSQL", "Docker"],
            education="B.Tech Computer Science",
            cover_note=(
                "Six years of Python services, mostly Flask rather than Django, backed by "
                "PostgreSQL. I ran the schema migration for a 300M-row orders table and cut "
                "our nightly batch from 6 hours to 40 minutes. I have read Django's ORM but "
                "not shipped it in production."
            ),
        ),
    ),
]


def installed_chat_models(base_url: str) -> list[str]:
    response = httpx.get(f"{base_url}/api/tags", timeout=10)
    response.raise_for_status()
    names = [m["name"] for m in response.json().get("models", [])]
    return [n for n in names if "embed" not in n.lower()]


def main() -> int:
    settings = get_settings()
    settings.screening_provider = "ollama"

    try:
        models = sys.argv[1:] or installed_chat_models(settings.ollama_base_url)
    except httpx.HTTPError as exc:
        print(f"Cannot reach Ollama at {settings.ollama_base_url}: {exc}")
        return 1

    if not models:
        print("No chat models installed. Try: ollama pull llama3")
        return 1

    cutoff = float(JOB.thresholds["score_cutoff"])
    print(f"Job: {JOB.title} — must-have {', '.join(JOB.required_skills)}, {JOB.min_years_experience:g}+ yrs")
    print(f"Strictness '{JOB.strictness}' -> shortlist cutoff {cutoff:g}")
    print(f"Testing {len(models)} model(s) on {len(CASES)} candidates.\n")

    summary: list[dict] = []

    for model in models:
        settings.screening_model = model
        backends._backend = None
        print(f"=== {model} ===")

        correct = false_pos = false_neg = in_band = disagreed = capped = 0
        elapsed_total = 0.0
        failed = False

        for label, (low, high), should_shortlist, candidate in CASES:
            rules = apply_rules(candidate, JOB)
            start = time.perf_counter()
            try:
                result = llm.score_candidate(JOB, candidate, rules)
            except llm.ScoringError as exc:
                print(f"  {label:24s} FAILED: {str(exc)[:100]}")
                failed = True
                break
            elapsed_total += time.perf_counter() - start

            shortlisted = result.fit_score >= cutoff
            if shortlisted == should_shortlist:
                correct += 1
                verdict = "correct"
            elif shortlisted:
                false_pos += 1
                verdict = "FALSE POSITIVE"
            else:
                false_neg += 1
                verdict = "false negative"

            in_band += low <= result.fit_score <= high
            disagreed += result._raw_recommendation != result.recommendation
            capped += result._raw_fit_score is not None

            raw = f" (model said {result._raw_fit_score}, capped)" if result._raw_fit_score else ""
            print(
                f"  {label:24s} score {result.fit_score:3d}{raw} -> "
                f"{'shortlist' if shortlisted else 'reject   '}  {verdict}"
            )

        if failed:
            print()
            continue

        avg = elapsed_total / len(CASES)
        print(
            f"  -> {correct}/{len(CASES)} correct decisions "
            f"({false_pos} false positive, {false_neg} false negative), "
            f"{in_band}/{len(CASES)} in band, "
            f"{disagreed} label/score contradiction(s), {capped} evidence-capped, "
            f"{avg:.1f}s per candidate\n"
        )
        summary.append(
            {
                "model": model,
                "correct": correct,
                "false_pos": false_pos,
                "in_band": in_band,
                "avg": avg,
            }
        )

    if summary:
        summary.sort(key=lambda r: (-r["correct"], r["false_pos"], r["avg"]))
        print(f"Ranked by shortlist decision accuracy (n={len(CASES)}):")
        for r in summary:
            print(
                f"  {r['model']:24s} {r['correct']}/{len(CASES)} correct  "
                f"{r['false_pos']} false-pos  {r['in_band']}/{len(CASES)} in band  "
                f"{r['avg']:6.1f}s/candidate"
            )
        best = summary[0]
        print(f"\nSuggested SCREENING_MODEL={best['model']}")
        if best["avg"] > 60:
            faster = [r for r in summary[1:] if r["avg"] < best["avg"] / 2]
            if faster:
                alt = faster[0]
                print(
                    f"  (that model is slow at {best['avg']:.0f}s/candidate — "
                    f"{alt['model']} is {best['avg'] / alt['avg']:.1f}x faster at "
                    f"{alt['correct']}/{len(CASES)} correct, if throughput matters more)"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
