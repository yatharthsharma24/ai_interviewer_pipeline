"""End-to-end verification of every layer, against a live in-process server.

Unlike ``smoke_test.py`` (pure units, no HTTP), this drives the actual FastAPI app the way
the dashboard does: real routes, real database writes, real serialisation. It uses throwaway
databases so it never touches your real data.

Anything needing outside credentials — Google, a real OpenAI call — is reported as
SKIP with the reason, not quietly passed.

    python scripts/verify_all.py            # offline, free, no API calls
    python scripts/verify_all.py --online   # also exercise OpenAI (costs a few cents)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ONLINE = "--online" in sys.argv

TMP_MAIN = ROOT / "data" / "_verify_main.db"
TMP_INT = ROOT / "data" / "_verify_int.db"
for path in (TMP_MAIN, TMP_INT):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{TMP_MAIN.as_posix()}"
os.environ["INTERVIEW_DATABASE_URL"] = f"sqlite:///{TMP_INT.as_posix()}"
os.environ["NOTIFY_DRY_RUN"] = "true"

os.environ["PROCTOR_LOOK_AWAY_SECONDS"] = "4"
os.environ["PROCTOR_NO_FACE_SECONDS"] = "8"
os.environ["PROCTOR_TAB_HIDDEN_SECONDS"] = "3"
os.environ["PROCTOR_MAX_VIOLATIONS"] = "4"

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Candidate, CandidateStatus  # noqa: E402

passed: list[str] = []
failed: list[str] = []
skipped: list[str] = []


def ok(label: str, condition: bool, detail: str = "") -> bool:
    if condition:
        passed.append(label)
        print(f"  PASS  {label}{f' — {detail}' if detail else ''}")
    else:
        failed.append(label)
        print(f"  FAIL  {label}{f' — {detail}' if detail else ''}")
    return bool(condition)


def skip(label: str, why: str) -> None:
    skipped.append(label)
    print(f"  SKIP  {label} — {why}")


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


client = TestClient(app)

_vs = get_settings()
if _vs.admin_password:
    client.auth = (_vs.admin_username, _vs.admin_password)

with client:
    section("Infrastructure")
    r = client.get("/health")
    ok("health endpoint", r.status_code == 200 and r.json()["status"] == "ok")

    r = client.get("/openapi.json")
    spec = r.json()
    ops = sum(len(v) for v in spec["paths"].values())
    ok("OpenAPI schema builds", r.status_code == 200, f"{len(spec['paths'])} paths, {ops} operations")

    r = client.get("/system/status")
    status = r.json()
    ok("system status responds", r.status_code == 200, f"overall={status['overall']}")
    named = {c["name"] for c in status["checks"]}
    ok(
        "status covers every component",
        {"Candidate database", "Interview database", "Screening model",
         "OpenAI (voice interview)", "Google (forms + email)",
         "Message delivery", "Interview join URL"} <= named,
        f"{len(named)} checks",
    )
    ok(
        "database checks are healthy",
        all(c["state"] == "ok" for c in status["checks"]
            if c["name"] in {"Candidate database", "Interview database"}),
    )
    for check in status["checks"]:
        if check["state"] in ("error", "warn", "off"):
            print(f"        {check['state']:5s} {check['name']}: {check['detail']}")

    section("Admin dashboard")
    r = client.get("/admin")
    ok("dashboard page serves", r.status_code == 200 and "Interview Pipeline" in r.text)
    ok("dashboard js serves", client.get("/admin/app.js").status_code == 200)
    ok("dashboard css serves", client.get("/admin/styles.css").status_code == 200)
    r = client.get("/", follow_redirects=False)
    ok("root redirects to dashboard", r.status_code in (307, 308) and "/admin" in r.headers.get("location", ""))

    section("Part 1 — jobs and screening")
    r = client.post("/jobs", json={
        "title": "Senior Backend Engineer", "department": "Platform", "location": "Bengaluru",
        "min_years_experience": 4, "required_skills": ["Python", "Django", "PostgreSQL"],
        "preferred_skills": ["Docker"], "strictness": "balanced",
        "description": "Own and scale payments and billing.",
    })
    ok("create job", r.status_code == 201)
    job_id = r.json()["id"]

    ok("list jobs", len(client.get("/jobs").json()) == 1)
    ok("get job", client.get(f"/jobs/{job_id}").json()["title"] == "Senior Backend Engineer")
    ok("patch job", client.patch(f"/jobs/{job_id}", json={"strictness": "strict"}).json()["strictness"] == "strict")
    client.patch(f"/jobs/{job_id}", json={"strictness": "balanced"})
    ok("bad field key rejected", client.post("/jobs", json={"title": "X", "required_fields": ["nope"]}).status_code == 422)
    ok("missing job 404s", client.get("/jobs/99999").status_code == 404)
    ok("form ops need a form", client.post(f"/jobs/{job_id}/sync").status_code == 400,
       "sync without a linked form is refused")

    roster = [
        ("Asha Rao", 92, 7.0, ["Python", "Django", "PostgreSQL", "Docker"], "Led the sharding migration at PayFlow."),
        ("Ravi Kumar", 75, 4.5, ["Python", "Django", "PostgreSQL"], "Built Django APIs for an e-commerce catalogue."),
        ("Priya Nair", 70, 6.0, ["Python", "Django", "PostgreSQL"], "Ran a 300M-row schema migration."),
        ("Sam Iyer", 58, 5.0, ["Python", "Django", "PostgreSQL"], "Worked on backend services."),
    ]
    with SessionLocal() as s:
        for index, (name, score, years, skills, note) in enumerate(roster):
            s.add(Candidate(
                job_id=job_id, response_id=f"v{index}", full_name=name,
                email=f"{name.split()[0].lower()}@example.com", phone=f"+9198765432{index}0",
                years_experience=years, skills=skills, fit_score=score,
                recommendation="yes", cover_note=note, resume_url="https://example.com/cv.pdf",
                status=CandidateStatus.shortlisted.value,
                assessment={"concerns": ["No evidence of leading a team"], "strengths": ["Deep Django"],
                            "matched_required_skills": skills, "missing_required_skills": []},
            ))
        s.commit()

    stats = client.get(f"/jobs/{job_id}/stats").json()
    ok("job stats", stats["total"] == 4 and stats["by_status"]["shortlisted"] == 4, str(stats["by_status"]))
    ok("list candidates", len(client.get(f"/jobs/{job_id}/candidates").json()) == 4)
    ok("filter by status", len(client.get(f"/jobs/{job_id}/candidates?status=shortlisted").json()) == 4)
    ok("shortlist endpoint", len(client.get(f"/jobs/{job_id}/shortlist").json()) == 4)
    ok("candidate detail", client.get("/candidates/1").json()["full_name"] == "Asha Rao")
    ok("candidates sorted by score", [c["full_name"] for c in client.get(f"/jobs/{job_id}/candidates").json()][0] == "Asha Rao")

    section("Part 2 — rounds, scheduling, notification")
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    r = client.post(f"/jobs/{job_id}/rounds", json={
        "name": "Technical Round 1", "acceptable_score": 70, "start_date": tomorrow,
        "day_start_time": "10:00", "day_end_time": "11:00", "slot_minutes": 30,
        "mode": "online", "timezone": "Asia/Kolkata",
    })
    ok("create round", r.status_code == 201)
    round_id = r.json()["id"]

    ok("list rounds", len(client.get(f"/jobs/{job_id}/rounds").json()) == 1)
    ok("set difficulty", client.patch(f"/rounds/{round_id}", json={"difficulty": "hard"}).json()["difficulty"] == "hard")

    prev = client.post(f"/rounds/{round_id}/schedule", json={"apply": False}).json()
    ok("schedule preview splits on the bar", prev["invited"] == 3 and prev["rejected"] == 1,
       f"{prev['invited']} in, {prev['rejected']} out")
    ok("preview persists nothing", len(prev["slots"]) == 0)
    ok("preview explains the timetable", "slot(s)" in prev["summary"], prev["summary"])

    applied = client.post(f"/rounds/{round_id}/schedule", json={"apply": True}).json()
    ok("apply creates slots", len(applied["slots"]) == 3)

    slots = client.get(f"/rounds/{round_id}/slots").json()
    ok("slots readable (the Part 3 handoff)", len(slots) == 3)
    ok("slot carries the embedded candidate", slots[0]["candidate"]["full_name"] == "Asha Rao")
    ok("slot times are timezone-aware UTC",
       slots[0]["scheduled_start"].endswith("Z") or "+00:00" in slots[0]["scheduled_start"],
       slots[0]["scheduled_start"])
    ok("day rolls over when full",
       slots[2]["scheduled_start"][:10] != slots[0]["scheduled_start"][:10],
       "2 slots/day, so the third moves to the next working day")
    ok("below-the-bar candidate rejected",
       len(client.get(f"/jobs/{job_id}/candidates?status=rejected_round").json()) == 1)

    n = client.post(f"/rounds/{round_id}/notify", json={"dry_run": True}).json()
    ok("dry-run notify reports what would be sent", n["sent"] == 3 and n["dry_run"] is True, str(n))

    after = client.get(f"/rounds/{round_id}/slots").json()
    ok("dry run leaves slots untouched", all(s["status"] == "pending" for s in after),
       ", ".join(s["status"] for s in after))
    ok("dry run does not mark candidates scheduled",
       len(client.get(f"/jobs/{job_id}/candidates?status=scheduled").json()) == 0)
    ok("dry run leaves the round un-notified",
       client.get(f"/rounds/{round_id}").json()["status"] != "notified")

    n2 = client.post(f"/rounds/{round_id}/notify", json={"dry_run": True}).json()
    ok("dry run is repeatable", n2["sent"] == 3, "a second dry run still reaches everyone")

    section("Part 3 — AI interview")
    if ONLINE:
        r = client.post(f"/rounds/{round_id}/interviews/prepare",
                        json={"plan_questions": True, "fetch_resumes": False})
        prepared = r.json() if r.status_code == 200 else []
        ok("prepare with question planning", r.status_code == 200 and len(prepared) == 3,
           f"{len(prepared)} interviews" if r.status_code == 200 else str(r.json())[:120])
        if prepared:
            plan = prepared[0]["question_plan"]
            ok("plan has questions", len(plan.get("questions", [])) > 0,
               f"{len(plan.get('questions', []))} questions")
    else:
        r = client.post(f"/rounds/{round_id}/interviews/prepare",
                        json={"plan_questions": False, "fetch_resumes": False})
        prepared = r.json() if r.status_code == 200 else []
        ok("prepare interviews (no planning)", r.status_code == 200 and len(prepared) == 3,
           f"{len(prepared)} interviews")
        skip("question planning", "needs an OpenAI call; re-run with --online")

    if prepared:
        interview_id = prepared[0]["id"]
        token = prepared[0]["join_url"].rsplit("/", 1)[-1]

        ok("interviews listed for the round", len(client.get(f"/rounds/{round_id}/interviews").json()) == 3)
        ok("join links are unique", len({p["join_url"] for p in prepared}) == 3)
        ok("interview bound to the right candidate",
           prepared[0]["candidate_id"] == slots[0]["candidate_id"])

        page = client.get(f"/interview/{token}")
        ok("candidate call page renders", page.status_code == 200 and "Interview" in page.text)
        ok("page carries the candidate's name", "Asha" in page.text)
        ok("unknown token 404s", client.get("/interview/not-a-real-token").status_code == 404)

        from sqlalchemy import select

        from app.interview.db import InterviewSession
        from app.interview.models import Interview

        with InterviewSession() as ivs:
            row = ivs.scalars(select(Interview).where(Interview.access_token == token)).first()
            good = dict(row.resume_snapshot)
            row.resume_snapshot = {**good, "candidate_id": 4242}
            ivs.commit()
            ok("wrong dossier refused (409)", client.get(f"/interview/{token}").status_code == 409)
            row.resume_snapshot = good
            ivs.commit()
        ok("valid dossier accepted again", client.get(f"/interview/{token}").status_code == 200)

        for speaker, text in [
            ("interviewer", "Tell me about the sharding migration you led."),
            ("candidate", "We sharded on merchant id because billing queries are scoped to one "
                          "merchant, which avoids cross-shard joins. Hot merchants can skew a shard."),
            ("interviewer", "How did you handle the hot shard problem?"),
            ("candidate", "Honestly we did not fully solve it. We added read replicas for the top "
                          "ten merchants. A composite key would have been better but was a bigger migration."),
        ]:
            client.post(f"/interview/{token}/transcript",
                        json={"speaker": speaker, "text": text, "at_seconds": 30.0})

        e = client.post(f"/interview/{token}/event",
                        json={"kind": "looked_away", "at_seconds": 70, "duration_seconds": 6.0}).json()
        ok("sustained violation counted", e["counted"] is True and e["violation_count"] == 1)
        ok("candidate is warned", bool(e["warning"]), e["warning"])
        ok("one violation does not end the call", e["should_end"] is False)

        e2 = client.post(f"/interview/{token}/event",
                         json={"kind": "tab_hidden", "at_seconds": 80, "duration_seconds": 0.4}).json()
        ok("brief blip not counted", e2["counted"] is False and e2["violation_count"] == 1)

        for at in (90, 100, 110):
            e3 = client.post(f"/interview/{token}/event",
                             json={"kind": "tab_hidden", "at_seconds": at, "duration_seconds": 9.0}).json()
        ok("repeated violations end the call", e3["should_end"] is True, f"after {e3['violation_count']}")

        detail = client.get(f"/interviews/{interview_id}").json()
        ok("transcript stored", len(detail["turns"]) == 4)
        ok("turns are ordered", [t["sequence"] for t in detail["turns"]] == [1, 2, 3, 4])
        ok("proctoring events stored", len(detail["events"]) == 5)
        ok("call auto-terminated", detail["status"] == "terminated", detail["status"])
        ok("integrity note rolled up", "tab_hidden" in (detail["integrity_note"] or ""))

        if ONLINE:
            with InterviewSession() as ivs:
                row = ivs.scalars(select(Interview).where(Interview.access_token == token)).first()
                row.status = "pending"
                row.ended_at = None
                ivs.commit()
            r = client.post(f"/interview/{token}/session")
            if r.status_code != 200:
                ok("voice session opens", False, str(r.json())[:160])
            else:
                d = r.json()
                ok("voice session opens", True, f"provider={d['provider']} model={d['model']}")
                if d["provider"] == "openai":
                    ok("openai session carries an ephemeral secret",
                       (d.get("client_secret") or "").startswith("ek_"))
                else:
                    ok("gemini session carries a relay path, not a key",
                       d.get("client_secret") is None and bool(d.get("live_path")),
                       str(d.get("live_path")))
                ok("proctor config sent to the browser", "max_violations" in d["proctor"])

            g = client.post(f"/interviews/{interview_id}/grade")
            if g.status_code == 200:
                rep = g.json()
                ok("grading writes a report",
                   rep["overall_rating"] is not None and bool(rep["summary"]),
                   f"{rep['overall_rating']}/100 — {rep['recommendation']}")
                ok("per-competency scores present", len(rep["ratings"].get("competencies", [])) > 0)
                ok("status becomes graded", rep["status"] == "graded")
                ok("the grading provider is recorded",
                   rep.get("providers", {}).get("grading") in ("openai", "gemini"),
                   str(rep.get("providers")))
            else:
                ok("grading writes a report", False, str(g.json())[:160])
        else:
            skip("voice session mint", "needs a live API call; re-run with --online")
            skip("grading", "needs a live API call; re-run with --online")

    section("Model providers (Part 3 failover)")
    from app.interview.providers import configured_providers  # noqa: E402
    from app.interview.realtime import live_provider_order  # noqa: E402

    _text_providers = configured_providers()
    _live_providers = live_provider_order()
    ok("at least one text provider configured", bool(_text_providers), ", ".join(_text_providers) or "none")
    ok("at least one voice provider configured", bool(_live_providers), ", ".join(_live_providers) or "none")
    if len(_live_providers) < 2:
        skip(
            "voice failover",
            "only "
            + (_live_providers[0] if _live_providers else "no provider")
            + " is configured — set both OPENAI_API_KEY and GEMINI_API_KEY for failover",
        )
    else:
        ok("voice failover available", True, " then ".join(_live_providers))

    _page = (ROOT / "app" / "interview" / "static" / "call.html").read_text(encoding="utf-8")
    _js = re.search(r'<script type="module">(.*?)</script>', _page, re.S)
    if not _js:
        ok("call page script found", False)
    elif not shutil.which("node"):
        skip("call page JavaScript parses", "node is not installed")
    else:
        _src = _js.group(1)
        for _k, _v in {
            "__TOKEN__": "tok", "__CANDIDATE_NAME__": "Test",
            "__JOB_TITLE__": "Role", "__ALREADY_DONE__": "false", "__END_REASON__": "",
        }.items():
            _src = _src.replace(_k, _v)
        _tmp = ROOT / "data" / "_verify_call.mjs"
        _tmp.write_text(_src, encoding="utf-8")
        _proc = subprocess.run(
            ["node", "--check", str(_tmp)], capture_output=True, text=True
        )
        _tmp.unlink(missing_ok=True)
        ok("call page JavaScript parses", _proc.returncode == 0, _proc.stderr.strip()[:160])

    if ONLINE and get_settings().gemini_api_key:
        import httpx as _httpx

        _s = get_settings()

        try:
            _r = _httpx.post(
                f"{_s.gemini_base_url.rstrip('/')}/v1beta/models/"
                f"{_s.gemini_model}:generateContent",
                headers={"x-goog-api-key": _s.gemini_api_key},
                json={
                    "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                    "generationConfig": {"maxOutputTokens": 1},
                },
                timeout=45,
            )
            _msg = ""
            if _r.status_code >= 400:
                try:
                    _msg = _r.json().get("error", {}).get("message", _r.text)[:150]
                except ValueError:
                    _msg = _r.text[:150]
            ok(
                f"Gemini text model {_s.gemini_model} is usable",
                _r.status_code == 200,
                _msg or "generated",
            )
        except Exception as exc:
            ok(f"Gemini text model {_s.gemini_model} is usable", False, str(exc)[:150])

        try:
            _lr = _httpx.get(
                f"{_s.gemini_base_url.rstrip('/')}/v1beta/models",
                headers={"x-goog-api-key": _s.gemini_api_key},
                params={"pageSize": 200},
                timeout=20,
            )
            _bidi = {
                m.get("name", "").removeprefix("models/")
                for m in _lr.json().get("models", [])
                if "bidiGenerateContent" in (m.get("supportedGenerationMethods") or [])
            }
            ok(
                f"Gemini live model {_s.gemini_live_model} supports bidi",
                _s.gemini_live_model in _bidi,
                f"{len(_bidi)} live-capable models visible",
            )
        except Exception as exc:
            ok("Gemini live model supports bidi", False, str(exc)[:150])

        try:
            from app.interview.grading import InterviewReport
            from app.interview.providers import PROVIDERS as _P

            _raw = _P["gemini"].complete_json(
                "You assess a completed job interview from its transcript.",
                "ROLE: Backend Engineer\n\nTRANSCRIPT\n"
                "[00:05] INTERVIEWER: Tell me about your sharding work.\n"
                "[00:31] CANDIDATE: We split billing on tenant id and dual-wrote for two weeks.\n",
                "interview_report",
                InterviewReport.model_json_schema(),
            )
            _rep = InterviewReport.model_validate(json.loads(_raw))
            ok("Gemini grading returns a schema-valid report", True,
               f"{_rep.overall_rating}/100 {_rep.recommendation}")
        except Exception as exc:
            ok("Gemini grading returns a schema-valid report", False,
               f"{type(exc).__name__}: {str(exc)[:140]}")
    elif not get_settings().gemini_api_key:
        skip("Gemini reachability", "GEMINI_API_KEY is not set")
    else:
        skip("Gemini reachability", "re-run with --online")

    if not prepared:
        skip("gemini session negotiation", "no interview was prepared to negotiate for")
    else:
        from types import SimpleNamespace

        from app.interview import realtime as _rt

        _gem_token = prepared[1]["join_url"].rsplit("/", 1)[-1]
        _real_rt_settings = _rt.get_settings
        _rt.get_settings = lambda: SimpleNamespace(
            openai_api_key=None,
            gemini_api_key="verify-only-not-a-real-key",
            interview_provider="gemini",
            gemini_live_model="gemini-2.5-flash-native-audio-preview-09-2025",
        )
        try:
            _r = client.post(f"/interview/{_gem_token}/session")
            _body = _r.json() if _r.status_code == 200 else {}
            ok("gemini session negotiates", _r.status_code == 200,
               "" if _body else f"HTTP {_r.status_code}: {_r.text[:120]}")
            ok("gemini session says gemini", _body.get("provider") == "gemini",
               str(_body.get("provider")))
            ok("gemini session carries no client secret", _body.get("client_secret") is None)
            ok(
                "gemini relay points at our own host",
                _body.get("live_path") == f"/interview/{_gem_token}/live",
                str(_body.get("live_path")),
            )
            ok(
                "gemini audio rates supplied",
                (_body.get("input_sample_rate"), _body.get("output_sample_rate")) == (16000, 24000),
            )
            ok("gemini api key never reaches the browser",
               "verify-only-not-a-real-key" not in _r.text)
        finally:
            _rt.get_settings = _real_rt_settings

        with InterviewSession() as _ivs:
            _row = _ivs.scalars(
                select(Interview).where(Interview.access_token == _gem_token)
            ).first()
            ok("the serving provider is recorded on the interview",
               (_row.providers or {}).get("live") == "gemini", str(_row.providers))

    try:
        with client.websocket_connect("/interview/definitely-not-a-token/live"):
            ok("relay socket refuses an unknown token", False, "the socket was accepted")
    except Exception:
        ok("relay socket refuses an unknown token", True)

    section("External services (not exercised here)")
    skip("Google Forms create/read", "needs OAuth credentials — run `python -m app.cli google-login`")
    skip("Gmail send", "needs OAuth credentials")
    skip("Live voice call", "needs a real browser with a camera and microphone")

    section("Cleanup")
    ok("delete round", client.delete(f"/rounds/{round_id}").status_code == 204)
    ok("delete job cascades", client.delete(f"/jobs/{job_id}").status_code == 204)
    ok("job is gone", client.get(f"/jobs/{job_id}").status_code == 404)

from app.db import engine as _main_engine  # noqa: E402
from app.interview.db import interview_engine as _iv_engine  # noqa: E402

_main_engine.dispose()
_iv_engine.dispose()
for path in (TMP_MAIN, TMP_INT):
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"\n(note: could not remove {path.name}: {exc})")

print("\n" + "=" * 62)
print(f"  {len(passed)} passed   {len(failed)} failed   {len(skipped)} skipped")
if failed:
    print("\nFAILED:")
    for label in failed:
        print(f"  - {label}")
if skipped:
    print("\nSkipped (need credentials or hardware):")
    for label in skipped:
        print(f"  - {label}")
print("=" * 62)
sys.exit(1 if failed else 0)
