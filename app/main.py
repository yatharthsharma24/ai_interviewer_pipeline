"""FastAPI service for the whole pipeline.

Part 1: job setup, Google Form automation, screening.
Part 2: interview rounds — score bar, slot allocation, email invitations.
Part 3: the live AI voice interview — call page, transcript, proctoring, grading.

Docs: http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session, init_db
from app.field_map import FIELDS
from app.google_api import forms as forms_api
from app.google_api.auth import GoogleAuthError
from app.google_api.gmail import MailError, send_form_link
from app.models import (
    Candidate,
    CandidateStatus,
    InterviewRound,
    InterviewSlot,
    JobOpening,
    JobStatus,
    SlotStatus,
)
from app.notify.channels import ChannelError, build_channels
from app.notify.rounds import allocate_round, apply_score_bar, commit_score_bar, notify_round
from app.scheduling import SchedulingError, SlotWindow, describe_window
from app.schemas import (
    CandidateRead,
    CreateFormRequest,
    FieldInfo,
    FormLinkResponse,
    JobCreate,
    JobRead,
    JobUpdate,
    LinkFormRequest,
    NotifyRequest,
    NotifyResponse,
    RoundCreate,
    RoundRead,
    RoundUpdate,
    ScheduleRequest,
    ScheduleResponse,
    ScreenRequest,
    ScreenResponse,
    SendLinkRequest,
    SlotRead,
    SyncResponse,
)
from app.admin.auth import admin_auth_middleware
from app.admin.web import router as admin_router
from app.interview.db import init_interview_db
from app.interview.web import router as interview_router
from app.screening.llm import ScoringError
from app.screening.pipeline import PipelineError, screen_job, sync_responses

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_settings()
    init_db()
    init_interview_db()
    yield


app = FastAPI(
    title="Interview Pipeline",
    description="Screening, interview scheduling and notifications, and AI voice interviews.",
    version="0.3.0",
    lifespan=lifespan,
)

app.middleware("http")(admin_auth_middleware)

app.include_router(interview_router)
app.include_router(admin_router)


def _get_job(session: Session, job_id: int) -> JobOpening:
    job = session.get(JobOpening, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")
    return job


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/fields", response_model=list[FieldInfo])
def list_fields() -> list[FieldInfo]:
    """The canonical candidate fields the form builder and screener understand."""
    return [
        FieldInfo(
            key=f.key,
            title=f.title,
            qtype=f.qtype,
            description=f.description,
            default_required=f.default_required,
        )
        for f in FIELDS
    ]


@app.post("/jobs", response_model=JobRead, status_code=201)
def create_job(payload: JobCreate, session: Session = Depends(get_session)) -> JobOpening:
    job = JobOpening(**payload.model_dump(mode="json"))
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@app.get("/jobs", response_model=list[JobRead])
def list_jobs(session: Session = Depends(get_session)) -> list[JobOpening]:
    return list(session.scalars(select(JobOpening).order_by(JobOpening.created_at.desc())).all())


@app.get("/jobs/{job_id}", response_model=JobRead)
def get_job(job_id: int, session: Session = Depends(get_session)) -> JobOpening:
    return _get_job(session, job_id)


@app.patch("/jobs/{job_id}", response_model=JobRead)
def update_job(
    job_id: int, payload: JobUpdate, session: Session = Depends(get_session)
) -> JobOpening:
    job = _get_job(session, job_id)
    for key, value in payload.model_dump(mode="json", exclude_unset=True).items():
        setattr(job, key, value)
    session.commit()
    session.refresh(job)
    return job


@app.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: int, session: Session = Depends(get_session)) -> None:
    job = _get_job(session, job_id)
    session.delete(job)
    session.commit()


@app.post("/jobs/{job_id}/form", response_model=FormLinkResponse)
def create_form(
    job_id: int, payload: CreateFormRequest, session: Session = Depends(get_session)
) -> FormLinkResponse:
    """Generate a Google Form from the job spec and attach it to the job."""
    job = _get_job(session, job_id)
    try:
        info = forms_api.create_form_for_job(job, payload.field_keys)
    except GoogleAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except forms_api.FormError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    job.form_id = info["form_id"]
    job.form_url = info["form_url"]
    job.form_edit_url = info["form_edit_url"]
    job.question_map = info["question_map"]
    job.status = JobStatus.open.value
    session.commit()

    return FormLinkResponse(**info)


@app.post("/jobs/{job_id}/form/link", response_model=FormLinkResponse)
def link_form(
    job_id: int, payload: LinkFormRequest, session: Session = Depends(get_session)
) -> FormLinkResponse:
    """Attach a form the admin built by hand, fuzzy-mapping its questions to our fields."""
    job = _get_job(session, job_id)
    try:
        info = forms_api.inspect_form(forms_api.extract_form_id(payload.form_ref))
    except GoogleAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except forms_api.FormError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    job.form_id = info["form_id"]
    job.form_url = info["form_url"]
    job.form_edit_url = info["form_edit_url"]
    job.question_map = info["question_map"]
    job.status = JobStatus.open.value
    session.commit()

    return FormLinkResponse(**info)


@app.post("/jobs/{job_id}/form/send")
def send_link(
    job_id: int, payload: SendLinkRequest, session: Session = Depends(get_session)
) -> dict[str, object]:
    job = _get_job(session, job_id)
    try:
        return send_form_link(
            job,
            payload.recipients,
            subject=payload.subject,
            body=payload.body,
            bcc=payload.bcc,
        )
    except GoogleAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except MailError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/jobs/{job_id}/sync", response_model=SyncResponse)
def sync(job_id: int, session: Session = Depends(get_session)) -> SyncResponse:
    """Pull every form response into the database."""
    job = _get_job(session, job_id)
    try:
        result = sync_responses(session, job)
    except GoogleAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except (PipelineError, forms_api.FormError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SyncResponse(**result.__dict__)


@app.post("/jobs/{job_id}/screen", response_model=ScreenResponse)
def screen(
    job_id: int, payload: ScreenRequest, session: Session = Depends(get_session)
) -> ScreenResponse:
    """Filter incomplete responses, apply hard rules, then score the survivors."""
    job = _get_job(session, job_id)
    try:
        result = screen_job(session, job, use_llm=payload.use_llm, rescreen=payload.rescreen)
    except ScoringError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ScreenResponse(**result.__dict__)


@app.post("/jobs/{job_id}/run", response_model=ScreenResponse)
def sync_and_screen(
    job_id: int, payload: ScreenRequest, session: Session = Depends(get_session)
) -> ScreenResponse:
    """Sync then screen — the one call a scheduler should hit."""
    sync(job_id, session)
    return screen(job_id, payload, session)


@app.get("/jobs/{job_id}/candidates", response_model=list[CandidateRead])
def list_candidates(
    job_id: int,
    status: CandidateStatus | None = Query(default=None),
    min_score: int | None = Query(default=None, ge=0, le=100),
    session: Session = Depends(get_session),
) -> list[Candidate]:
    _get_job(session, job_id)
    query = select(Candidate).where(Candidate.job_id == job_id)
    if status is not None:
        query = query.where(Candidate.status == status.value)
    if min_score is not None:
        query = query.where(Candidate.fit_score >= min_score)
    query = query.order_by(Candidate.fit_score.desc().nullslast(), Candidate.id)
    return list(session.scalars(query).all())


@app.get("/jobs/{job_id}/shortlist", response_model=list[CandidateRead])
def shortlist(job_id: int, session: Session = Depends(get_session)) -> list[Candidate]:
    """The output of Part 1 — the candidates Part 2 will pick up."""
    _get_job(session, job_id)
    query = (
        select(Candidate)
        .where(
            Candidate.job_id == job_id,
            Candidate.status == CandidateStatus.shortlisted.value,
        )
        .order_by(Candidate.fit_score.desc().nullslast(), Candidate.id)
    )
    return list(session.scalars(query).all())


@app.get("/candidates/{candidate_id}", response_model=CandidateRead)
def get_candidate(candidate_id: int, session: Session = Depends(get_session)) -> Candidate:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"Candidate {candidate_id} not found.")
    return candidate


def _get_round(session: Session, round_id: int) -> InterviewRound:
    round_ = session.get(InterviewRound, round_id)
    if round_ is None:
        raise HTTPException(status_code=404, detail=f"Round {round_id} not found.")
    return round_


@app.post("/jobs/{job_id}/rounds", response_model=RoundRead, status_code=201)
def create_round(
    job_id: int, payload: RoundCreate, session: Session = Depends(get_session)
) -> InterviewRound:
    _get_job(session, job_id)
    round_ = InterviewRound(job_id=job_id, **payload.model_dump())
    session.add(round_)
    session.commit()
    session.refresh(round_)
    return round_


@app.get("/jobs/{job_id}/rounds", response_model=list[RoundRead])
def list_rounds(job_id: int, session: Session = Depends(get_session)) -> list[InterviewRound]:
    _get_job(session, job_id)
    return list(
        session.scalars(
            select(InterviewRound)
            .where(InterviewRound.job_id == job_id)
            .order_by(InterviewRound.id)
        ).all()
    )


@app.get("/rounds/{round_id}", response_model=RoundRead)
def get_round(round_id: int, session: Session = Depends(get_session)) -> InterviewRound:
    return _get_round(session, round_id)


@app.patch("/rounds/{round_id}", response_model=RoundRead)
def update_round(
    round_id: int, payload: RoundUpdate, session: Session = Depends(get_session)
) -> InterviewRound:
    round_ = _get_round(session, round_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(round_, key, value)
    session.commit()
    session.refresh(round_)
    return round_


@app.post("/rounds/{round_id}/schedule", response_model=ScheduleResponse)
def schedule_round(
    round_id: int, payload: ScheduleRequest, session: Session = Depends(get_session)
) -> ScheduleResponse:
    """Apply the acceptable score and allocate slots. Set `apply` to persist."""
    round_ = _get_round(session, round_id)
    bar = apply_score_bar(session, round_)

    window = SlotWindow(
        start_date=round_.start_date,
        day_start_time=round_.day_start_time,
        day_end_time=round_.day_end_time,
        slot_minutes=round_.slot_minutes,
        break_minutes=round_.break_minutes,
        skip_weekends=round_.skip_weekends,
        timezone=round_.timezone,
    )
    try:
        summary = describe_window(window, len(bar.invited))
    except SchedulingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    slots: list[InterviewSlot] = []
    if payload.apply and bar.invited:
        commit_score_bar(session, round_, bar)
        slots = allocate_round(session, round_, bar.invited)

    return ScheduleResponse(
        applied=payload.apply,
        invited=len(bar.invited),
        rejected=len(bar.rejected),
        unscored=len(bar.unscored),
        summary=summary,
        slots=[SlotRead.model_validate(s) for s in slots],
        rejected_candidates=[CandidateRead.model_validate(c) for c in bar.rejected],
        unscored_candidates=[CandidateRead.model_validate(c) for c in bar.unscored],
    )


@app.post("/rounds/{round_id}/notify", response_model=NotifyResponse)
def notify_round_endpoint(
    round_id: int, payload: NotifyRequest, session: Session = Depends(get_session)
) -> NotifyResponse:
    """Send each scheduled candidate their own interview details."""
    round_ = _get_round(session, round_id)
    try:
        channels = build_channels(
            use_email=payload.use_email,
            dry_run=payload.dry_run,
        )
    except ChannelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    dry = any("DRY RUN" in c.describe() for c in channels)

    join_urls: dict[int, str] = {}
    try:
        from app.interview.db import interview_session_scope
        from app.interview.models import Interview
        from app.interview.service import join_url as build_join_url

        with interview_session_scope() as interviews:
            for interview in interviews.scalars(
                select(Interview).where(Interview.round_id == round_.id)
            ).all():
                join_urls[interview.candidate_id] = build_join_url(interview)
    except Exception:
        logger.warning("Could not attach interview links to round %s", round_id, exc_info=True)

    result = notify_round(
        session, round_, channels,
        only_pending=not payload.retry_all, join_urls=join_urls, dry_run=dry,
    )
    return NotifyResponse(
        sent=result.sent,
        partial=result.partial,
        failed=result.failed,
        skipped=result.skipped,
        dry_run=dry,
        errors=result.errors,
    )


@app.delete("/rounds/{round_id}", status_code=204)
def delete_round(round_id: int, session: Session = Depends(get_session)) -> None:
    """Delete a round and its slots. Interview records in the other database survive."""
    round_ = _get_round(session, round_id)
    session.delete(round_)
    session.commit()


@app.get("/jobs/{job_id}/stats")
def job_stats(job_id: int, session: Session = Depends(get_session)) -> dict:
    """Counts per candidate status — what the dashboard funnel is drawn from."""
    _get_job(session, job_id)
    rows = session.execute(
        select(Candidate.status, func.count(Candidate.id))
        .where(Candidate.job_id == job_id)
        .group_by(Candidate.status)
    ).all()
    by_status = {status: count for status, count in rows}
    rounds = session.scalar(
        select(func.count(InterviewRound.id)).where(InterviewRound.job_id == job_id)
    )
    return {
        "job_id": job_id,
        "total": sum(by_status.values()),
        "by_status": by_status,
        "rounds": rounds or 0,
    }


@app.get("/rounds/{round_id}/slots", response_model=list[SlotRead])
def list_slots(
    round_id: int,
    include_cancelled: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> list[InterviewSlot]:
    """The interview schedule — this is what Part 3 reads."""
    _get_round(session, round_id)
    query = select(InterviewSlot).where(InterviewSlot.round_id == round_id)
    if not include_cancelled:
        query = query.where(InterviewSlot.status != SlotStatus.cancelled.value)
    query = query.order_by(InterviewSlot.scheduled_start, InterviewSlot.id)
    return list(session.scalars(query).all())
