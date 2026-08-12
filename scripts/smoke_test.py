"""End-to-end check of everything that does not need Google or the OpenAI API.

Seeds a throwaway in-memory database with a job and five fabricated applicants, runs the
deterministic screening stages, and asserts each one lands where it should.

    python scripts/smoke_test.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.update(
    {
        "PROCTOR_LOOK_AWAY_SECONDS": "4",
        "PROCTOR_NO_FACE_SECONDS": "8",
        "PROCTOR_TAB_HIDDEN_SECONDS": "3",
        "PROCTOR_MAX_VIOLATIONS": "4",
        "DEFAULT_TIMEZONE": "Asia/Kolkata",
    }
)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.field_map import match_question_title
from app.models import Base, Candidate, CandidateStatus, JobOpening, Strictness
from app.parsing import parse_currency, parse_list, parse_notice_period_days, parse_years
from app.screening.pipeline import response_to_fields, screen_candidate

failures: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    if actual == expected:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {actual!r}, expected {expected!r}")
        failures.append(label)


print("\nparsing")
check("'4.5 years' -> 4.5", parse_years("4.5 years"), 4.5)
check("'3 years 6 months' -> 3.5", parse_years("3 years 6 months"), 3.5)
check("'Fresher' -> 0", parse_years("Fresher"), 0.0)
check("'' -> None", parse_years(""), None)
check("'Immediate' -> 0 days", parse_notice_period_days("Immediate"), 0)
check("'2 months' -> 60 days", parse_notice_period_days("2 months"), 60)
check("'45' -> 45 days", parse_notice_period_days("45"), 45)
check("'12 LPA' -> 1200000", parse_currency("12 LPA"), 1_200_000.0)
check("'90k' -> 90000", parse_currency("90k"), 90_000.0)
check("skills split", parse_list(["Python, Django", "Python"]), ["Python", "Django"])

print("\nquestion-title matching (for hand-built forms)")
check("'Your Full Name'", match_question_title("Your Full Name"), "full_name")
check("'Total yrs of experience'", match_question_title("Total yrs of experience"), "years_experience")
check("'Mobile Number'", match_question_title("Mobile Number"), "phone")
check("'Link to your CV'", match_question_title("Link to your CV"), "resume_url")
check("'Favourite colour' -> unmapped", match_question_title("Favourite colour"), None)

print("\nresponse mapping")
question_map = {"full_name": "q1", "years_experience": "q2", "skills": "q3"}
raw = {
    "responseId": "r1",
    "answers": {
        "q1": {"textAnswers": {"answers": [{"value": "Asha Rao"}]}},
        "q2": {"textAnswers": {"answers": [{"value": "6 years"}]}},
        "q3": {"textAnswers": {"answers": [{"value": "Python"}, {"value": "Django"}]}},
        "q9": {"textAnswers": {"answers": [{"value": "unknown question"}]}},
    },
}
mapped, unmapped = response_to_fields(raw, question_map)
check("name mapped", mapped["full_name"], "Asha Rao")
check("years coerced", mapped["years_experience"], 6.0)
check("skills as list", mapped["skills"], ["Python", "Django"])
check("stray answer counted", unmapped, 1)

print("\nscore reconciliation (guards against small-model inconsistency)")
from app.screening.llm import (  # noqa: E402
    UNSUPPORTED_EVIDENCE_CAP,
    FitAssessment,
    _apply_evidence_cap,
    _output_schema,
    recommendation_for_score,
)

check("95 -> strong_yes", recommendation_for_score(95), "strong_yes")
check("84 -> yes", recommendation_for_score(84), "yes")
check("69 -> maybe", recommendation_for_score(69), "maybe")
check("25 -> no", recommendation_for_score(25), "no")


def _assessment(score: int, concrete: bool) -> FitAssessment:
    return FitAssessment(
        fit_score=score, describes_concrete_work=concrete, recommendation="strong_yes",
        matched_required_skills=[], missing_required_skills=[], strengths=[], concerns=[],
        seniority_assessment="x", rationale="y",
    )


capped = _assessment(85, concrete=False)
_apply_evidence_cap(capped)
check("no concrete work -> capped", capped.fit_score, UNSUPPORTED_EVIDENCE_CAP)
check("pre-cap score preserved", capped._raw_fit_score, 85)
check("cap explained in concerns", len(capped.concerns), 1)

kept = _assessment(85, concrete=True)
_apply_evidence_cap(kept)
check("concrete work -> untouched", kept.fit_score, 85)
check("no phantom raw score", kept._raw_fit_score, None)

low = _assessment(30, concrete=False)
_apply_evidence_cap(low)
check("already below cap -> untouched", low.fit_score, 30)

schema = _output_schema()
check("schema forbids extra keys", schema.get("additionalProperties"), False)
check("every property required", set(schema["properties"]), set(schema["required"]))
check("private attrs stay out of schema", "_raw_fit_score" in schema["properties"], False)
_banned = {"minimum", "maximum", "minLength", "maxLength", "multipleOf", "pattern"}
_found: set[str] = set()


def _walk(node: object) -> None:
    if isinstance(node, dict):
        _found.update(_banned & node.keys())
        for value in node.values():
            _walk(value)
    elif isinstance(node, list):
        for value in node:
            _walk(value)


_walk(schema)
check("no strict-mode-unsupported keywords", _found, set())

print("\nscreening")
engine = create_engine("sqlite://")
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, expire_on_commit=False)
session = Session()

job = JobOpening(
    title="Backend Engineer",
    min_years_experience=3,
    required_skills=["Python", "Django", "PostgreSQL"],
    preferred_skills=["Docker"],
    max_notice_period_days=60,
    strictness=Strictness.balanced.value,
    required_fields=["full_name", "email", "phone", "years_experience", "skills", "resume_url"],
)
session.add(job)
session.commit()


def add(**kwargs) -> Candidate:
    candidate = Candidate(job_id=job.id, **kwargs)
    session.add(candidate)
    session.commit()
    return candidate


base = {
    "email": "a@example.com",
    "phone": "+91 9876543210",
    "resume_url": "https://drive.google.com/file/d/abc/view",
}

strong = add(
    response_id="s1", full_name="Asha Rao", years_experience=6.0,
    skills=["Python", "Django", "PostgreSQL", "Docker"], **base,
)
blank_phone = add(
    response_id="s2", full_name="Blank Phone", years_experience=5.0,
    email="b@example.com", phone=None,
    skills=["Python", "Django", "PostgreSQL"],
    resume_url="https://example.com/cv.pdf",
)
bad_email = add(
    response_id="s3", full_name="Bad Email", years_experience=5.0,
    email="not-an-email", phone="+91 9000000000",
    skills=["Python", "Django", "PostgreSQL"],
    resume_url="https://example.com/cv.pdf",
)
junior = add(
    response_id="s4", full_name="Too Junior", years_experience=0.5,
    skills=["Python", "Django", "PostgreSQL"], **base,
)
wrong_stack = add(
    response_id="s5", full_name="Wrong Stack", years_experience=8.0,
    skills=["Java", "Spring"], **base,
)
long_notice = add(
    response_id="s6", full_name="Long Notice", years_experience=5.0,
    skills=["Python", "Django", "PostgreSQL"], notice_period_days=120, **base,
)

for candidate in (strong, blank_phone, bad_email, junior, wrong_stack, long_notice):
    screen_candidate(session, job, candidate, use_llm=False)

check("complete + qualified -> shortlisted", strong.status, CandidateStatus.shortlisted.value)
check("blank phone -> rejected_incomplete", blank_phone.status, CandidateStatus.rejected_incomplete.value)
check("malformed email -> rejected_incomplete", bad_email.status, CandidateStatus.rejected_incomplete.value)
check("0.5 yrs vs 3 yr floor -> rejected_rules", junior.status, CandidateStatus.rejected_rules.value)
check("no must-have skills -> rejected_rules", wrong_stack.status, CandidateStatus.rejected_rules.value)
check("120-day notice -> rejected_rules", long_notice.status, CandidateStatus.rejected_rules.value)
check("rejection is explained", bool(junior.rule_failures), True)

prose = add(
    response_id="s7", full_name="Prose Only", years_experience=5.0, skills=[],
    cover_note="I have shipped Python and Django services on PostgreSQL for four years.",
    **base,
)
job.required_fields = ["full_name", "email", "phone", "years_experience", "resume_url"]
session.commit()
screen_candidate(session, job, prose, use_llm=False)
check("skills found in free text", prose.status, CandidateStatus.shortlisted.value)

job.strictness = Strictness.very_strict.value
session.commit()
partial = add(
    response_id="s8", full_name="Two Of Three", years_experience=4.0,
    skills=["Python", "Django"], **base,
)
screen_candidate(session, job, partial, use_llm=False)
check("very_strict demands every must-have", partial.status, CandidateStatus.rejected_rules.value)

job.strictness = Strictness.lenient.value
session.commit()
screen_candidate(session, job, partial, use_llm=False)
check("lenient lets 2/3 through", partial.status, CandidateStatus.shortlisted.value)

from app.notify.channels import DryRunChannel  # noqa: E402
from app.notify.rounds import (  # noqa: E402
    allocate_round,
    apply_score_bar,
    commit_score_bar,
    notify_round,
)
from app.scheduling import SchedulingError, SlotWindow, allocate_slots  # noqa: E402

print("\npart 2 — slot allocation")
window = SlotWindow(
    start_date=dt.date(2026, 8, 14),
    day_start_time=dt.time(10, 0),
    day_end_time=dt.time(11, 0),
    slot_minutes=30,
)
check("2 slots fit in 10:00-11:00", window.slots_per_day(), 2)
allocated = allocate_slots(window, 3)
kolkata = ZoneInfo("Asia/Kolkata")
check("first slot 10:00 local", allocated[0][0].astimezone(kolkata).strftime("%H:%M"), "10:00")
check("second slot 10:30 local", allocated[1][0].astimezone(kolkata).strftime("%H:%M"), "10:30")
check(
    "third rolls to Monday (weekend skipped)",
    allocated[2][0].astimezone(kolkata).strftime("%a %d"),
    "Mon 17",
)
check("stored as UTC", allocated[0][0].strftime("%H:%M %Z"), "04:30 UTC")
check("slot end respects duration", allocated[0][1] - allocated[0][0], dt.timedelta(minutes=30))

with_break = SlotWindow(
    start_date=dt.date(2026, 8, 14), day_start_time=dt.time(10, 0),
    day_end_time=dt.time(12, 0), slot_minutes=30, break_minutes=15,
)
check("break shrinks slots/day", with_break.slots_per_day(), 3)

no_weekend_skip = SlotWindow(
    start_date=dt.date(2026, 8, 14), day_start_time=dt.time(10, 0),
    day_end_time=dt.time(11, 0), slot_minutes=30, skip_weekends=False,
)
check(
    "skip_weekends=False uses Saturday",
    allocate_slots(no_weekend_skip, 3)[2][0].astimezone(kolkata).strftime("%a"),
    "Sat",
)

for label, bad_window in (
    ("slot longer than the day", SlotWindow(dt.date(2026, 8, 14), dt.time(10, 0), dt.time(10, 15), 30)),
    ("end before start", SlotWindow(dt.date(2026, 8, 14), dt.time(17, 0), dt.time(10, 0), 30)),
    ("unknown timezone", SlotWindow(dt.date(2026, 8, 14), dt.time(10, 0), dt.time(17, 0), 30, timezone="Mars/Olympus")),
):
    try:
        bad_window.validate()
        check(label + " rejected", "accepted", "rejected")
    except SchedulingError:
        check(label + " rejected", "rejected", "rejected")

print("\npart 2 — score bar, scheduling, notification")
from app.models import InterviewRound, InterviewSlot, RoundStatus, SlotStatus  # noqa: E402

job2 = JobOpening(
    title="Backend Engineer", required_skills=["Python"],
    responsibilities=[], preferred_skills=[], required_fields=[],
)
session.add(job2)
session.commit()

roster = [("Asha Rao", 92), ("Ravi Kumar", 75), ("Priya Nair", 70), ("Sam Iyer", 64)]
people = []
for index, (name, score) in enumerate(roster):
    person = Candidate(
        job_id=job2.id, response_id=f"p2-{index}", full_name=name,
        email=f"c{index}@example.com", phone=f"+9198765432{index}0",
        fit_score=score, status=CandidateStatus.shortlisted.value, skills=["Python"],
    )
    session.add(person)
    people.append(person)
session.add(
    Candidate(
        job_id=job2.id, response_id="p2-x", full_name="No Score", email="ns@example.com",
        phone="+919876500000", fit_score=None,
        status=CandidateStatus.shortlisted.value, skills=["Python"],
    )
)
session.commit()

round_ = InterviewRound(
    job_id=job2.id, name="Technical Round 1", acceptable_score=70,
    start_date=dt.date(2026, 8, 14), day_start_time=dt.time(10, 0),
    day_end_time=dt.time(11, 0), slot_minutes=30, timezone="Asia/Kolkata",
    mode="online", location="https://meet.example.com/x",
)
session.add(round_)
session.commit()

bar = apply_score_bar(session, round_)
check("above the bar are invited", [c.full_name for c in bar.invited],
      ["Asha Rao", "Ravi Kumar", "Priya Nair"])
check("below the bar are rejected", [c.full_name for c in bar.rejected], ["Sam Iyer"])
check("unscored held out, not guessed", [c.full_name for c in bar.unscored], ["No Score"])

commit_score_bar(session, round_, bar)
check("rejection persisted", people[3].status, CandidateStatus.rejected_round.value)
check("unscored left untouched", bar.unscored[0].status, CandidateStatus.shortlisted.value)

slots = allocate_round(session, round_, bar.invited)
check("one slot each", len(slots), 3)
check("slots ordered by score", [s.candidate.full_name for s in slots],
      ["Asha Rao", "Ravi Kumar", "Priya Nair"])
check("nothing sent yet", {s.status for s in slots}, {SlotStatus.pending.value})

email_channel = DryRunChannel("email")
notified = notify_round(session, round_, [email_channel])
check("all three notified", notified.sent, 3)
check("no failures", notified.failed, 0)
check("emails rendered", len(email_channel.sent), 3)
check("candidate marked scheduled", people[0].status, CandidateStatus.scheduled.value)
check("local time in email", "10:00" in email_channel.sent[0]["body"], True)
check("timezone named in email", "Asia/Kolkata" in email_channel.sent[0]["body"], True)

again = notify_round(session, round_, [email_channel])
check("re-run sends nothing (idempotent)", again.sent, 0)
check("re-run skips the notified", again.skipped, 3)

dry_round = InterviewRound(
    job_id=job2.id, name="Dry Run Check", acceptable_score=70,
    start_date=dt.date(2026, 8, 17), day_start_time=dt.time(10, 0),
    day_end_time=dt.time(17, 0), slot_minutes=30, timezone="Asia/Kolkata",
)
session.add(dry_round)
session.commit()
dry_bar = apply_score_bar(session, dry_round)
dry_slots = allocate_round(session, dry_round, dry_bar.invited)
before_statuses = [s.status for s in dry_slots]

simulated = notify_round(session, dry_round, [DryRunChannel("email")], dry_run=True)
session.expire_all()
check("dry run still reports what would be sent", simulated.sent, len(dry_slots))
check("dry run persists no slot change",
      [session.get(InterviewSlot, s.id).status for s in dry_slots], before_statuses)
check("dry run persists no round change",
      session.get(InterviewRound, dry_round.id).status, RoundStatus.scheduled.value)

for_real = notify_round(session, dry_round, [DryRunChannel("email")], dry_run=False)
check("a real send after a dry run still reaches everyone", for_real.sent, len(dry_slots))

missing_contact = Candidate(
    job_id=job2.id, response_id="p2-nc", full_name="No Contact",
    email=None, phone=None, fit_score=95,
    status=CandidateStatus.shortlisted.value, skills=["Python"],
)
session.add(missing_contact)
session.commit()
bar2 = apply_score_bar(session, round_)
allocate_round(session, round_, bar2.invited)
outcome = notify_round(session, round_, [DryRunChannel("email")])
check("missing contact details do not abort the run", outcome.failed + outcome.sent >= 1, True)

print("\npart 3 — identity binding (the failure that must never happen)")
from app.interview.models import (  # noqa: E402
    DIFFICULTY_PROFILES,
    Difficulty,
    Interview,
    InterviewBase,
    InterviewStatus,
    TranscriptTurn,
)
from app.interview.resume import build_dossier, direct_download_url, dossier_text  # noqa: E402
from app.interview.service import (  # noqa: E402
    IdentityMismatch,
    InterviewError,
    end_interview,
    record_turn,
    record_violation,
    resolve_interview,
)

iv_engine = create_engine("sqlite://")
InterviewBase.metadata.create_all(iv_engine)
iv_session = sessionmaker(bind=iv_engine, expire_on_commit=False)()

interviewee = people[0]
dossier = build_dossier(interviewee, job2)
check("dossier carries candidate_id", dossier["candidate_id"], interviewee.id)
check("dossier carries the job", dossier["job"]["title"], "Backend Engineer")

record = Interview(
    slot_id=slots[0].id, candidate_id=interviewee.id, round_id=round_.id, job_id=job2.id,
    access_token="test-token-abc", candidate_name=interviewee.full_name,
    candidate_email=interviewee.email, resume_snapshot=dossier, difficulty="hard",
)
iv_session.add(record)
iv_session.commit()

resolved, matched = resolve_interview(session, iv_session, "test-token-abc")
check("valid token resolves", matched.id, interviewee.id)

try:
    resolve_interview(session, iv_session, "not-a-real-token")
    check("unknown token refused", "accepted", "refused")
except InterviewError:
    check("unknown token refused", "refused", "refused")

record.resume_snapshot = {**dossier, "candidate_id": 9999}
iv_session.commit()
try:
    resolve_interview(session, iv_session, "test-token-abc")
    check("dossier for another candidate refused", "accepted", "refused")
except IdentityMismatch:
    check("dossier for another candidate refused", "refused", "refused")

record.resume_snapshot = dossier
record.candidate_email = "someone.else@example.com"
iv_session.commit()
try:
    resolve_interview(session, iv_session, "test-token-abc")
    check("changed email refused", "accepted", "refused")
except IdentityMismatch:
    check("changed email refused", "refused", "refused")
record.candidate_email = interviewee.email
iv_session.commit()

print("\npart 3 — transcript and proctoring")
record_turn(iv_session, record, "interviewer", "Tell me about your Django work.", 5.0)
record_turn(iv_session, record, "candidate", "I ran the billing service at PayFlow.", 12.0)
check("turns stored in order", [t.sequence for t in record.turns], [1, 2])
check("blank turn ignored", record_turn(iv_session, record, "candidate", "   ", 20.0), None)
check("transcript renders with timestamps", "[00:05] INTERVIEWER" in record.transcript_text(), True)

_, end_now = record_violation(iv_session, record, "looked_away", 30.0, 6.0)
check("sustained look-away counts", record.violation_count, 1)
check("one violation does not end the call", end_now, False)

_, end_now = record_violation(iv_session, record, "tab_hidden", 40.0, 0.5)
check("brief tab switch is not counted", record.violation_count, 1)

for offset in (50.0, 60.0, 70.0):
    _, end_now = record_violation(iv_session, record, "tab_hidden", offset, 9.0)
check("repeated violations end the call", end_now, True)

end_interview(iv_session, record, reason="proctor: too many violations")
check("terminated status recorded", record.status, InterviewStatus.terminated.value)
check("integrity note summarises", "tab_hidden x3" in (record.integrity_note or ""), True)

print("\npart 3 — planning inputs")
check("four difficulty levels", len(DIFFICULTY_PROFILES), 4)
check(
    "harder means more questions",
    DIFFICULTY_PROFILES[Difficulty.expert]["questions"]
    > DIFFICULTY_PROFILES[Difficulty.easy]["questions"],
    True,
)
check(
    "drive share link rewritten for download",
    direct_download_url("https://drive.google.com/file/d/ABC123xyz/view?usp=sharing"),
    "https://drive.google.com/uc?export=download&id=ABC123xyz",
)
check(
    "plain url passes through",
    direct_download_url("https://example.com/cv.pdf"),
    "https://example.com/cv.pdf",
)
rendered = dossier_text(dossier)
check("dossier text names the role", "Backend Engineer" in rendered, True)
check("dossier text keeps screening notes internal", "not shared with the candidate" in rendered, True)

from app.interview.grading import InterviewReport  # noqa: E402
from app.interview.plan import QuestionPlan  # noqa: E402

for name, model in (("question plan", QuestionPlan), ("interview report", InterviewReport)):
    s = model.model_json_schema()
    check(f"{name} schema forbids extras", s.get("additionalProperties"), False)
    check(f"{name} requires every property", set(s["properties"]), set(s["required"]))

print("\nadmin access control")
import base64 as _b64  # noqa: E402

from app.admin.auth import check_basic_auth, is_public  # noqa: E402

check("candidate call page is public", is_public("/interview/abc123"), True)
check("candidate sub-routes are public", is_public("/interview/abc123/session"), True)
check("admin interview list is NOT public", is_public("/interviews/5"), False)
check("admin interviews root is NOT public", is_public("/interviews"), False)
check("health is public", is_public("/health"), True)

for guarded in ("/admin", "/jobs", "/docs", "/openapi.json", "/system/status", "/candidates/1"):
    check(f"{guarded} is guarded", is_public(guarded), False)

_header = "Basic " + _b64.b64encode(b"admin:s3cret").decode()
check("correct credentials accepted", check_basic_auth(_header, "admin", "s3cret"), True)
check("wrong password rejected", check_basic_auth(_header, "admin", "other"), False)
check("wrong username rejected", check_basic_auth(_header, "root", "s3cret"), False)
check("missing header rejected", check_basic_auth(None, "admin", "s3cret"), False)
check("empty header rejected", check_basic_auth("", "admin", "s3cret"), False)
check("non-basic scheme rejected", check_basic_auth("Bearer abc", "admin", "s3cret"), False)
check("malformed base64 rejected", check_basic_auth("Basic !!!not-b64!!!", "admin", "s3cret"), False)
check(
    "credentials without a colon rejected",
    check_basic_auth("Basic " + _b64.b64encode(b"adminonly").decode(), "admin", "s3cret"),
    False,
)


print("\nschema migration (an existing database must survive an upgrade)")
from sqlalchemy import inspect as _sa_inspect, text as _sa_text  # noqa: E402

import app.migrate as _migrate  # noqa: E402
from app.migrate import ensure_columns, migrate, rename_columns  # noqa: E402

_migrate.RENAMED_COLUMNS = {
    "interview_slots": [
        ("sent_at", "email_sent_at"),
        ("message_id", "email_message_id"),
    ],
}

_mig_engine = create_engine("sqlite://")

with _mig_engine.begin() as _conn:
    _conn.execute(_sa_text(
        "CREATE TABLE interview_slots ("
        " id INTEGER PRIMARY KEY, round_id INTEGER, candidate_id INTEGER,"
        " sent_at DATETIME, message_id VARCHAR)"
    ))
    _conn.execute(_sa_text(
        "INSERT INTO interview_slots (id, round_id, candidate_id, sent_at,"
        " message_id) VALUES (1, 7, 42, '2026-08-01 10:00:00', 'MSG123')"
    ))

_renamed = rename_columns(_mig_engine)
_cols = {c["name"] for c in _sa_inspect(_mig_engine).get_columns("interview_slots")}
check("rename reports what it did", sorted(_renamed),
      ["interview_slots.message_id -> email_message_id",
       "interview_slots.sent_at -> email_sent_at"])
check("old column names are gone", {"sent_at", "message_id"} & _cols, set())
check("new column names exist", {"email_sent_at", "email_message_id"} <= _cols, True)

with _mig_engine.connect() as _conn:
    _row = _conn.execute(_sa_text("SELECT email_message_id FROM interview_slots WHERE id=1")).one()
check("delivery receipt survived the rename", _row[0], "MSG123")

check("rename is idempotent", rename_columns(_mig_engine), [])

_added = ensure_columns(_mig_engine, Base.metadata)
_cols = {c["name"] for c in _sa_inspect(_mig_engine).get_columns("interview_slots")}
check("missing columns were added", {"status", "notify_errors"} <= _cols, True)
check("add is idempotent", ensure_columns(_mig_engine, Base.metadata), [])
check("add did not re-add the renamed columns",
      any("email_sent_at" in c for c in _added), False)

_engine2 = create_engine("sqlite://")
with _engine2.begin() as _conn:
    _conn.execute(_sa_text(
        "CREATE TABLE interview_slots ("
        " id INTEGER PRIMARY KEY, sent_at DATETIME, message_id VARCHAR)"
    ))
    _conn.execute(_sa_text(
        "INSERT INTO interview_slots (id, message_id) VALUES (1, 'MSG999')"
    ))
migrate(_engine2, Base.metadata)
with _engine2.connect() as _conn:
    _kept = _conn.execute(
        _sa_text("SELECT email_message_id FROM interview_slots WHERE id=1")
    ).one()
check("migrate() renames before it adds", _kept[0], "MSG999")

_migrate.RENAMED_COLUMNS = {}

from app.interview.models import InterviewBase as _IB  # noqa: E402

_engine3 = create_engine("sqlite://")
_IB.metadata.create_all(_engine3)
with _engine3.begin() as _conn:
    _conn.execute(_sa_text("ALTER TABLE interviews DROP COLUMN providers"))
check("simulated old interviews.db lacks providers",
      "providers" in {c["name"] for c in _sa_inspect(_engine3).get_columns("interviews")}, False)
migrate(_engine3, _IB.metadata)
check("migrate adds providers to an old interviews.db",
      "providers" in {c["name"] for c in _sa_inspect(_engine3).get_columns("interviews")}, True)

for _e in (_mig_engine, _engine2, _engine3):
    _e.dispose()

print("\npart 3 — provider failover (OpenAI to Gemini)")
import app.interview.providers as _prov  # noqa: E402
from app.interview.providers import (  # noqa: E402
    AllProvidersFailed,
    ProviderError,
    to_gemini_schema,
)

_gem = to_gemini_schema(QuestionPlan.model_json_schema())
_flat = json.dumps(_gem)
check("gemini schema drops additionalProperties", "additionalProperties" in _flat, False)
check("gemini schema drops $defs/$ref", "$ref" in _flat or "$defs" in _flat, False)
check("gemini schema drops title", '"title"' in _flat, False)
check("gemini schema inlines the nested model",
      _gem["properties"]["questions"]["items"]["properties"]["topic"]["enum"][0], "experience")
check("gemini schema keeps required", _gem["required"], ["opening", "questions", "closing"])
check("gemini schema keeps descriptions",
      _gem["properties"]["opening"]["description"].startswith("How the interviewer"), True)
check("report schema converts too",
      "additionalProperties" in json.dumps(to_gemini_schema(InterviewReport.model_json_schema())),
      False)

_recursive = {
    "type": "object",
    "$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}},
    "properties": {"root": {"$ref": "#/$defs/Node"}},
}
try:
    to_gemini_schema(_recursive)
    check("recursive schema refused", "no error raised", "ValueError")
except ValueError:
    check("recursive schema refused", "ValueError", "ValueError")


class _FakeProvider:
    """Stands in for a real provider: either answers, or fails the way one would."""

    def __init__(self, name, reply=None, fail=None):
        self.name, self._reply, self._fail = name, reply, fail
        self.calls = 0

    def available(self):
        return True

    def describe(self):
        return self.name

    def complete_json(self, system, user, schema_name, schema):
        self.calls += 1
        if self._fail:
            raise ProviderError(self.name, self._fail)
        return self._reply


_saved_providers, _saved_order = _prov.PROVIDERS, _prov.provider_order


def _with_providers(providers, order):
    _prov.PROVIDERS = providers
    _prov.provider_order = lambda: order


try:
    good = _FakeProvider("gemini", reply='{"ok": true}')
    dead = _FakeProvider("openai", fail="401 invalid_api_key")
    _with_providers({"openai": dead, "gemini": good}, ["openai", "gemini"])

    parsed, used = _prov.complete_json("sys", "usr", schema_name="s", schema={})
    check("falls back when the primary fails", used, "gemini")
    check("primary was actually tried first", dead.calls, 1)
    check("fallback result is returned", parsed, {"ok": True})

    alive = _FakeProvider("openai", reply='{"ok": 1}')
    spare = _FakeProvider("gemini", reply='{"ok": 2}')
    _with_providers({"openai": alive, "gemini": spare}, ["openai", "gemini"])
    _, used = _prov.complete_json("sys", "usr", schema_name="s", schema={})
    check("working primary is used", used, "openai")
    check("fallback not called when primary works", spare.calls, 0)

    _with_providers({"openai": alive, "gemini": spare}, ["gemini", "openai"])
    _, used = _prov.complete_json("sys", "usr", schema_name="s", schema={})
    check("gemini can be primary", used, "gemini")

    junk = _FakeProvider("openai", reply="not json at all")
    rescue = _FakeProvider("gemini", reply='{"ok": true}')
    _with_providers({"openai": junk, "gemini": rescue}, ["openai", "gemini"])
    _, used = _prov.complete_json("sys", "usr", schema_name="s", schema={})
    check("invalid JSON falls through to the fallback", used, "gemini")

    _with_providers(
        {"openai": _FakeProvider("openai", fail="429 rate limit"),
         "gemini": _FakeProvider("gemini", fail="403 key disabled")},
        ["openai", "gemini"],
    )
    try:
        _prov.complete_json("sys", "usr", schema_name="s", schema={})
        check("all-providers-failed raises", "no error raised", "AllProvidersFailed")
    except AllProvidersFailed as exc:
        check("all-providers-failed raises", "AllProvidersFailed", "AllProvidersFailed")
        check("both reasons reported",
              "429 rate limit" in str(exc) and "403 key disabled" in str(exc), True)

    class _Unconfigured(_FakeProvider):
        def available(self):
            return False

    _with_providers({"openai": _Unconfigured("openai"), "gemini": _Unconfigured("gemini")},
                    ["openai", "gemini"])
    try:
        _prov.complete_json("sys", "usr", schema_name="s", schema={})
        check("no provider configured is named as such", "no error raised", "AllProvidersFailed")
    except AllProvidersFailed as exc:
        check("no provider configured is named as such",
              "No model provider is configured" in str(exc), True)
finally:
    _prov.PROVIDERS, _prov.provider_order = _saved_providers, _saved_order


from app.interview import realtime as _rt  # noqa: E402

_orig_rt_settings = _rt.get_settings


def _fake_rt_settings(**kw):
    base = {"openai_api_key": "sk-x", "gemini_api_key": None, "interview_provider": "openai"}
    base.update(kw)
    return lambda: SimpleNamespace(**base)


try:
    _rt.get_settings = _fake_rt_settings()
    check("live: openai only", _rt.live_provider_order(), ["openai"])
    _rt.get_settings = _fake_rt_settings(gemini_api_key="g-x")
    check("live: both keys, openai primary", _rt.live_provider_order(), ["openai", "gemini"])
    _rt.get_settings = _fake_rt_settings(gemini_api_key="g-x", interview_provider="gemini")
    check("live: both keys, gemini primary", _rt.live_provider_order(), ["gemini", "openai"])
    _rt.get_settings = _fake_rt_settings(openai_api_key=None, gemini_api_key="g-x")
    check("live: gemini only", _rt.live_provider_order(), ["gemini"])
    _rt.get_settings = _fake_rt_settings(openai_api_key=None, gemini_api_key=None)
    check("live: neither configured", _rt.live_provider_order(), [])
finally:
    _rt.get_settings = _orig_rt_settings


from app.interview.live_proxy import _TurnBuffer, _harvest_transcript  # noqa: E402

_stored: list = []
_record = lambda speaker, text: _stored.append((speaker, text))  # noqa: E731
_buf = _TurnBuffer()
for _frame in [
    {"serverContent": {"inputTranscription": {"text": "I built "}}},
    {"serverContent": {"inputTranscription": {"text": "a sharding layer."}}},
    {"serverContent": {"outputTranscription": {"text": "Got it. "}}},
    {"serverContent": {"outputTranscription": {"text": "What broke?"}}},
    {"serverContent": {"turnComplete": True}},
]:
    _harvest_transcript(_frame, _buf, _record)

check("streamed deltas are joined into turns", _stored,
      [("candidate", "I built a sharding layer."), ("interviewer", "Got it. What broke?")])
check("buffer is emptied after a flush", _buf.flush("candidate"), None)

_stored.clear()
_harvest_transcript({"serverContent": {"inputTranscription": {"text": "mid-answer"}}}, _buf, _record)
check("nothing stored before the turn completes", _stored, [])
check("an abandoned turn is still recoverable", _buf.flush_all(), [("candidate", "mid-answer")])

_stored.clear()
_harvest_transcript({"serverContent": {"inputTranscription": {"text": "wait, actually"}}}, _buf, _record)
_harvest_transcript({"serverContent": {"interrupted": True}}, _buf, _record)
check("an interruption flushes the turn", _stored, [("candidate", "wait, actually")])

for _junk in [{}, {"serverContent": None}, {"serverContent": {}}, {"setupComplete": {}},
              {"serverContent": {"inputTranscription": None}}]:
    _harvest_transcript(_junk, _buf, _record)
check("malformed frames are ignored, not raised", True, True)


_page = (ROOT / "app" / "interview" / "static" / "call.html").read_text(encoding="utf-8")
check("call page has both transports", "connectOpenAI" in _page and "connectGemini" in _page, True)
check("call page branches on provider", 'session.provider === "gemini"' in _page, True)
check("gemini path never carries an api key",
      "gemini_api_key" in _page or "x-goog-api-key" in _page, False)
check("gemini socket goes to our own host, not google",
      "generativelanguage.googleapis.com" in _page, False)

print("\npart 3 — grading falls over to Gemini when OpenAI is dead")
from app.interview.grading import grade_interview  # noqa: E402
from app.interview.service import grade_and_store  # noqa: E402

_grade_engine = create_engine("sqlite://")
InterviewBase.metadata.create_all(_grade_engine)
_GradeSession = sessionmaker(bind=_grade_engine, expire_on_commit=False)
_gsession = _GradeSession()

_graded_iv = Interview(
    slot_id=1, candidate_id=1, round_id=1, job_id=1,
    candidate_name="Asha Rao", candidate_email="asha@example.com",
    access_token="grade-failover-token", difficulty="medium",
    status=InterviewStatus.completed.value,
    resume_snapshot={"candidate_id": 1, "full_name": "Asha Rao", "email": "asha@example.com",
                     "job": {"title": "Backend Engineer", "required_skills": ["Python"]}},
    started_at=dt.datetime(2026, 8, 17, 4, 30, tzinfo=dt.timezone.utc),
    ended_at=dt.datetime(2026, 8, 17, 4, 52, tzinfo=dt.timezone.utc),
)
_gsession.add(_graded_iv)
_gsession.commit()
for _seq, (_who, _said) in enumerate([
    ("interviewer", "Tell me about the sharding work."),
    ("candidate", "We split the billing table on tenant id and backfilled over three weeks."),
], start=1):
    _gsession.add(TranscriptTurn(interview_id=_graded_iv.id, sequence=_seq, speaker=_who,
                                 text=_said, at_seconds=_seq * 30.0))
_gsession.commit()
_gsession.refresh(_graded_iv)

_REPORT = json.dumps({
    "overall_rating": 74,
    "recommendation": "hire",
    "summary": "Concrete, specific answers about a real migration.",
    "competencies": [{"competency": "Python depth", "level": "good", "score": 72,
                      "evidence": "Described the tenant-id split."}],
    "per_question": [{"question": "Tell me about the sharding work.",
                      "answer_quality": "good", "note": "Specific and owned."}],
    "strengths": ["Real migration experience"],
    "concerns": ["Short interview, limited evidence"],
    "red_flags": [],
    "communication": "Clear and well structured, no rambling.",
    "interview_completed": True,
})

_saved_providers, _saved_order = _prov.PROVIDERS, _prov.provider_order
try:
    _dead_openai = _FakeProvider("openai", fail="401 Incorrect API key provided")
    _live_gemini = _FakeProvider("gemini", reply=_REPORT)
    _with_providers({"openai": _dead_openai, "gemini": _live_gemini}, ["openai", "gemini"])

    _report, _used = grade_interview(_graded_iv)
    check("grading falls over to gemini", _used, "gemini")
    check("openai was tried first", _dead_openai.calls, 1)
    check("gemini produced the report", _report.overall_rating, 74)
    check("recommendation survives the fallback", _report.recommendation, "hire")
    check("competencies survive the fallback", len(_report.competencies), 1)

    _wild = json.loads(_REPORT)
    _wild["overall_rating"] = 150
    _wild["competencies"][0]["score"] = -20
    _with_providers({"openai": _FakeProvider("openai", fail="401"),
                     "gemini": _FakeProvider("gemini", reply=json.dumps(_wild))},
                    ["openai", "gemini"])
    _clamped, _ = grade_interview(_graded_iv)
    check("out-of-range overall clamped on the fallback path", _clamped.overall_rating, 100)
    check("out-of-range competency clamped too", _clamped.competencies[0].score, 0)

    _with_providers({"openai": _FakeProvider("openai", fail="401 Incorrect API key provided"),
                     "gemini": _FakeProvider("gemini", reply=_REPORT)},
                    ["openai", "gemini"])
    grade_and_store(_gsession, _graded_iv)
    check("report stored after failover", _graded_iv.overall_rating, 74)
    check("summary stored", bool(_graded_iv.summary), True)
    check("status becomes graded", _graded_iv.status, InterviewStatus.graded.value)
    check("graded_at set", _graded_iv.graded_at is not None, True)
    check("the grading provider is recorded", _graded_iv.providers.get("grading"), "gemini")

    _graded_iv.providers = {"plan": "openai", "live": "openai"}
    _gsession.commit()
    grade_and_store(_gsession, _graded_iv)
    check("earlier stages are not overwritten",
          _graded_iv.providers, {"plan": "openai", "live": "openai", "grading": "gemini"})

    _with_providers({"openai": _FakeProvider("openai", fail="401 Incorrect API key provided"),
                     "gemini": _FakeProvider("gemini", fail="403 API key not valid")},
                    ["openai", "gemini"])
    try:
        grade_interview(_graded_iv)
        check("both dead raises", "no error raised", "AllProvidersFailed")
    except AllProvidersFailed as exc:
        check("both dead raises", "AllProvidersFailed", "AllProvidersFailed")
        check("the error names both providers",
              "openai" in str(exc) and "gemini" in str(exc), True)
finally:
    _prov.PROVIDERS, _prov.provider_order = _saved_providers, _saved_order
    _gsession.close()
    _grade_engine.dispose()

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
    sys.exit(1)
print("All checks passed.")
