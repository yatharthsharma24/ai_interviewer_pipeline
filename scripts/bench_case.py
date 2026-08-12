"""Re-run a single benchmark case across models — for iterating on the prompt.

A full `bench_models.py` sweep takes ~25 minutes. When you change the prompt to fix one
specific failure, this re-tests just that case so you can see the effect in a minute or two.
Always confirm with a full sweep before committing the change: a prompt tweak that fixes one
case can quietly regress another.

    python scripts/bench_case.py "keyword stuffer"
    python scripts/bench_case.py "keyword stuffer" llama3:latest gemma4:latest
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.screening import backends, llm
from app.screening.rules import apply_rules
from scripts.bench_models import CASES, JOB, installed_chat_models


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/bench_case.py <case label> [model ...]")
        print("cases: " + ", ".join(repr(c[0]) for c in CASES))
        return 1

    label_arg = sys.argv[1].lower()
    matches = [c for c in CASES if c[0].lower() == label_arg]
    if not matches:
        print(f"No case named {sys.argv[1]!r}. Available: " + ", ".join(repr(c[0]) for c in CASES))
        return 1
    label, (low, high), should_shortlist, candidate = matches[0]

    settings = get_settings()
    settings.screening_provider = "ollama"
    models = sys.argv[2:] or installed_chat_models(settings.ollama_base_url)

    cutoff = float(JOB.thresholds["score_cutoff"])
    want = "shortlist" if should_shortlist else "reject"
    print(f"Case {label!r} — a human would {want} (cutoff {cutoff:g}, expected band {low}-{high})\n")

    rules = apply_rules(candidate, JOB)
    for model in models:
        settings.screening_model = model
        backends._backend = None
        start = time.perf_counter()
        try:
            result = llm.score_candidate(JOB, candidate, rules)
        except llm.ScoringError as exc:
            print(f"  {model:24s} FAILED: {str(exc)[:90]}")
            continue
        elapsed = time.perf_counter() - start

        shortlisted = result.fit_score >= cutoff
        ok = "correct" if shortlisted == should_shortlist else "WRONG"
        raw = f" (model said {result._raw_fit_score})" if result._raw_fit_score else ""
        print(
            f"  {model:24s} score {result.fit_score:3d}{raw} -> "
            f"{'shortlist' if shortlisted else 'reject   '}  {ok:7s} "
            f"concrete={str(result.describes_concrete_work):5s} {elapsed:5.1f}s"
        )
        print(f"    {result.rationale[:150]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
