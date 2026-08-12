"""HTTP surface for the live interview.

Two audiences on one router:

* **The candidate** — reaches ``/interview/{token}`` with a secret link, gets a page, a way
  to talk to the model, and endpoints to stream the transcript and proctoring events back.
  Everything here is authorised by the token alone.
* **The admin** — reads interviews, transcripts and reports under ``/interviews``.

The browser never holds an API key. On OpenAI it gets a short-lived client secret and talks
to OpenAI directly; on Gemini it gets a path to ``/interview/{token}/live`` on this server,
which relays the socket upstream (see ``live_proxy.py``).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.interview.db import get_interview_session
from app.interview.models import Interview, InterviewStatus, Speaker, ViolationType
from app.interview.live_proxy import proxy_gemini_live
from app.interview.realtime import RealtimeError, open_live_session
from app.interview.service import (
    IdentityMismatch,
    InterviewError,
    end_interview,
    grade_and_store,
    record_turn,
    record_violation,
    resolve_interview,
    start_interview,
)

logger = logging.getLogger(__name__)
router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "static"


class TurnIn(BaseModel):
    speaker: Speaker
    text: str
    at_seconds: float = Field(default=0.0, ge=0)


class EventIn(BaseModel):
    kind: ViolationType
    at_seconds: float = Field(default=0.0, ge=0)
    duration_seconds: float = Field(default=0.0, ge=0)
    detail: str | None = None


class EventOut(BaseModel):
    recorded: bool
    counted: bool
    violation_count: int
    should_end: bool
    warning: str | None = None


class EndIn(BaseModel):
    reason: str = "completed"


class SessionOut(BaseModel):
    provider: str
    model: str
    client_secret: str | None = None
    expires_at: int | None = None
    live_path: str | None = None
    input_sample_rate: int | None = None
    output_sample_rate: int | None = None
    candidate_name: str | None = None
    max_minutes: int
    proctor: dict


class TranscriptTurnOut(BaseModel):
    sequence: int
    speaker: str
    text: str
    at_seconds: float


class ProctoringEventOut(BaseModel):
    kind: str
    at_seconds: float
    duration_seconds: float
    counted: bool
    detail: str | None = None


class InterviewOut(BaseModel):
    id: int
    candidate_id: int
    candidate_name: str | None
    slot_id: int
    round_id: int
    job_id: int
    difficulty: str
    status: str
    join_url: str
    overall_rating: int | None
    recommendation: str | None
    summary: str | None
    ratings: dict
    violation_count: int
    integrity_note: str | None
    duration_seconds: float | None
    question_plan: dict | list
    providers: dict = Field(default_factory=dict)
    turns: list[TranscriptTurnOut] = Field(default_factory=list)
    events: list[ProctoringEventOut] = Field(default_factory=list)


def _load(main: Session, interviews: Session, token: str) -> Interview:
    try:
        interview, _ = resolve_interview(main, interviews, token)
    except IdentityMismatch as exc:
        logger.error("Identity mismatch on token: %s", exc)
        raise HTTPException(
            status_code=409,
            detail="This interview could not be verified. Please contact the hiring team.",
        ) from exc
    except InterviewError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return interview


@router.get("/interview/{token}", response_class=HTMLResponse)
def interview_page(
    token: str,
    main: Session = Depends(get_session),
    interviews: Session = Depends(get_interview_session),
) -> HTMLResponse:
    """The call page. Loading it does not start the interview — the candidate must click."""
    interview = _load(main, interviews, token)

    page = (STATIC_DIR / "call.html").read_text(encoding="utf-8")
    job = (interview.resume_snapshot or {}).get("job", {})
    already_done = interview.status in (
        InterviewStatus.completed.value,
        InterviewStatus.graded.value,
        InterviewStatus.terminated.value,
    )

    replacements = {
        "__TOKEN__": token,
        "__CANDIDATE_NAME__": interview.candidate_name or "there",
        "__JOB_TITLE__": job.get("title") or "the role",
        "__ALREADY_DONE__": "true" if already_done else "false",
        "__END_REASON__": interview.end_reason or "",
    }
    for key, value in replacements.items():
        page = page.replace(key, str(value))

    return HTMLResponse(page)


@router.post("/interview/{token}/session", response_model=SessionOut)
def create_session(
    token: str,
    main: Session = Depends(get_session),
    interviews: Session = Depends(get_interview_session),
) -> SessionOut:
    """Pick a voice provider, prepare the connection, and mark the interview started."""
    interview = _load(main, interviews, token)
    settings = get_settings()

    try:
        start_interview(interviews, interview)
    except InterviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        opened = open_live_session(interview)
    except RealtimeError as exc:
        logger.error("Could not open a voice session: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    interview.providers = {**(interview.providers or {}), "live": opened["provider"]}
    interviews.commit()

    return SessionOut(
        provider=opened["provider"],
        model=opened["model"],
        client_secret=opened.get("client_secret"),
        expires_at=opened.get("expires_at"),
        live_path=opened.get("live_path"),
        input_sample_rate=opened.get("input_sample_rate"),
        output_sample_rate=opened.get("output_sample_rate"),
        candidate_name=interview.candidate_name,
        max_minutes=settings.interview_max_minutes,
        proctor={
            "look_away_seconds": settings.proctor_look_away_seconds,
            "no_face_seconds": settings.proctor_no_face_seconds,
            "tab_hidden_seconds": settings.proctor_tab_hidden_seconds,
            "max_violations": settings.proctor_max_violations,
        },
    )


@router.websocket("/interview/{token}/live")
async def live_relay(
    websocket: WebSocket,
    token: str,
    main: Session = Depends(get_session),
    interviews: Session = Depends(get_interview_session),
) -> None:
    """Relay the candidate's audio to Gemini Live and the interviewer's audio back.

    Only reached when the session endpoint chose Gemini. The same token that authorises the
    page authorises this socket — and it is re-resolved here rather than trusted from the
    earlier request, because a WebSocket upgrade is a fresh connection.
    """
    try:
        interview, _ = resolve_interview(main, interviews, token)
    except (IdentityMismatch, InterviewError) as exc:
        logger.warning("Refusing a live socket: %s", exc)
        await websocket.close(code=4004, reason="unknown or unverified interview")
        return

    await websocket.accept()

    def _record(speaker: str, text: str) -> None:
        try:
            record_turn(interviews, interview, speaker, text, _elapsed(interview))
        except Exception:
            logger.exception("Could not store a transcript turn")

    try:
        await proxy_gemini_live(websocket, interviews, interview, record_turn=_record)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Live relay failed")
    finally:
        with contextlib.suppress(RuntimeError):
            await websocket.close()


def _elapsed(interview: Interview) -> float:
    """Seconds since the call started, for transcript ordering."""
    if not interview.started_at:
        return 0.0
    now = dt.datetime.now(dt.timezone.utc)
    return max(0.0, (now - interview.started_at).total_seconds())


@router.post("/interview/{token}/transcript", status_code=204)
def append_transcript(
    token: str,
    payload: TurnIn,
    main: Session = Depends(get_session),
    interviews: Session = Depends(get_interview_session),
) -> None:
    interview = _load(main, interviews, token)
    record_turn(interviews, interview, payload.speaker.value, payload.text, payload.at_seconds)


@router.post("/interview/{token}/event", response_model=EventOut)
def append_event(
    token: str,
    payload: EventIn,
    main: Session = Depends(get_session),
    interviews: Session = Depends(get_interview_session),
) -> EventOut:
    """Record a proctoring observation and tell the page whether to end the call."""
    interview = _load(main, interviews, token)
    settings = get_settings()

    event, should_end = record_violation(
        interviews,
        interview,
        payload.kind.value,
        payload.at_seconds,
        payload.duration_seconds,
        payload.detail,
    )

    warning = None
    if event.counted and not should_end:
        remaining = settings.proctor_max_violations - (interview.violation_count or 0)
        readable = {
            ViolationType.tab_hidden.value: "Please stay on this tab during the interview.",
            ViolationType.window_blurred.value: "Please keep this window focused.",
            ViolationType.looked_away.value: "Please look at the camera.",
            ViolationType.no_face.value: "We cannot see you — please face the camera.",
            ViolationType.multiple_faces.value: "More than one person is visible.",
            ViolationType.camera_off.value: "Your camera appears to be off.",
        }.get(payload.kind.value, "Please follow the interview rules.")
        warning = f"{readable} ({remaining} warning{'s' if remaining != 1 else ''} left)"

    if should_end:
        end_interview(interviews, interview, reason="proctor: too many violations")

    return EventOut(
        recorded=True,
        counted=event.counted,
        violation_count=interview.violation_count or 0,
        should_end=should_end,
        warning=warning,
    )


@router.post("/interview/{token}/end", status_code=204)
def finish(
    token: str,
    payload: EndIn,
    main: Session = Depends(get_session),
    interviews: Session = Depends(get_interview_session),
) -> None:
    interview = _load(main, interviews, token)
    end_interview(interviews, interview, reason=payload.reason)


def _to_out(interview: Interview) -> InterviewOut:
    from app.interview.service import join_url

    return InterviewOut(
        id=interview.id,
        candidate_id=interview.candidate_id,
        candidate_name=interview.candidate_name,
        slot_id=interview.slot_id,
        round_id=interview.round_id,
        job_id=interview.job_id,
        difficulty=interview.difficulty,
        status=interview.status,
        join_url=join_url(interview),
        overall_rating=interview.overall_rating,
        recommendation=interview.recommendation,
        summary=interview.summary,
        ratings=interview.ratings or {},
        violation_count=interview.violation_count or 0,
        integrity_note=interview.integrity_note,
        providers=interview.providers or {},
        duration_seconds=interview.duration_seconds(),
        question_plan=interview.question_plan or [],
        turns=[
            TranscriptTurnOut(
                sequence=t.sequence, speaker=t.speaker, text=t.text, at_seconds=t.at_seconds
            )
            for t in interview.turns
        ],
        events=[
            ProctoringEventOut(
                kind=e.kind,
                at_seconds=e.at_seconds,
                duration_seconds=e.duration_seconds,
                counted=bool(e.counted),
                detail=e.detail,
            )
            for e in interview.events
        ],
    )


class PrepareIn(BaseModel):
    difficulty: str | None = Field(
        default=None, description="Override the round's difficulty for this preparation."
    )
    plan_questions: bool = Field(default=True, description="Generate the question plan (API calls).")
    fetch_resumes: bool = Field(default=True, description="Try to download resume documents.")


@router.post("/rounds/{round_id}/interviews/prepare", response_model=list[InterviewOut])
def prepare(
    round_id: int,
    payload: PrepareIn,
    main: Session = Depends(get_session),
    interviews: Session = Depends(get_interview_session),
) -> list[InterviewOut]:
    """Create an AI interview for every scheduled candidate and mint their join links.

    Idempotent — a slot that already has an interview keeps its token, so a link already
    emailed to a candidate never stops working.
    """
    from app.interview.models import Difficulty
    from app.interview.service import prepare_interviews
    from app.models import InterviewRound

    round_ = main.get(InterviewRound, round_id)
    if round_ is None:
        raise HTTPException(status_code=404, detail=f"Round {round_id} not found.")

    if payload.difficulty:
        try:
            round_.difficulty = Difficulty(payload.difficulty).value
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown difficulty {payload.difficulty!r}. "
                f"Valid: {', '.join(d.value for d in Difficulty)}.",
            ) from exc
        main.commit()

    try:
        prepared = prepare_interviews(
            main,
            interviews,
            round_,
            plan_questions=payload.plan_questions,
            fetch_resumes=payload.fetch_resumes,
        )
    except InterviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Preparing interviews failed for round %s", round_id)
        raise HTTPException(status_code=502, detail=f"Preparation failed: {exc}") from exc

    return [_to_out(row) for row in prepared]


@router.get("/rounds/{round_id}/interviews", response_model=list[InterviewOut])
def list_round_interviews(
    round_id: int, interviews: Session = Depends(get_interview_session)
) -> list[InterviewOut]:
    rows = interviews.scalars(
        select(Interview).where(Interview.round_id == round_id).order_by(Interview.scheduled_start)
    ).all()
    return [_to_out(row) for row in rows]


@router.get("/interviews/{interview_id}", response_model=InterviewOut)
def get_interview(
    interview_id: int,
    include_transcript: bool = Query(default=True),
    interviews: Session = Depends(get_interview_session),
) -> InterviewOut:
    interview = interviews.get(Interview, interview_id)
    if interview is None:
        raise HTTPException(status_code=404, detail=f"Interview {interview_id} not found.")
    out = _to_out(interview)
    if not include_transcript:
        out.turns = []
    return out


@router.post("/interviews/{interview_id}/grade", response_model=InterviewOut)
def grade(
    interview_id: int, interviews: Session = Depends(get_interview_session)
) -> InterviewOut:
    """Write the summary and ratings from the stored transcript."""
    interview = interviews.get(Interview, interview_id)
    if interview is None:
        raise HTTPException(status_code=404, detail=f"Interview {interview_id} not found.")
    try:
        grade_and_store(interviews, interview)
    except InterviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Grading failed for interview %s", interview_id)
        raise HTTPException(status_code=502, detail=f"Grading failed: {exc}") from exc
    return _to_out(interview)
