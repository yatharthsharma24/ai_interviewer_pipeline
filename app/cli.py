"""Command-line front end.

    python -m app.cli --help

Typical first run:

    python -m app.cli init
    python -m app.cli google-login
    python -m app.cli new-job          # interactive: asks for the whole role spec
    python -m app.cli create-form 1
    python -m app.cli send-link 1 --to someone@example.com
    python -m app.cli run 1            # sync responses + screen them
    python -m app.cli shortlist 1
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import select

from app.config import EXPORT_DIR, get_settings
from app.db import init_db, session_scope
from app.field_map import DEFAULT_REQUIRED_FIELDS, FIELDS, FIELDS_BY_KEY
from app.google_api import forms as forms_api
from app.google_api.auth import GoogleAuthError, get_credentials
from app.google_api.gmail import MailError, send_form_link
from app.models import Candidate, CandidateStatus, JobOpening, JobStatus, Strictness
from app.screening.llm import ScoringError
from app.screening.pipeline import PipelineError, screen_job, sync_responses

app = typer.Typer(help="Interview pipeline — Part 1: job setup, forms, and screening.", no_args_is_help=True)
console = Console()

STATUS_STYLE = {
    CandidateStatus.shortlisted.value: "bold green",
    CandidateStatus.rejected_incomplete.value: "yellow",
    CandidateStatus.rejected_rules.value: "red",
    CandidateStatus.rejected_score.value: "red",
    CandidateStatus.new.value: "dim",
}


def _load_job(session, job_id: int) -> JobOpening:
    job = session.get(JobOpening, job_id)
    if job is None:
        console.print(f"[red]Job {job_id} not found.[/red]")
        raise typer.Exit(code=1)
    return job


def _csv_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


@app.command()
def init() -> None:
    """Create the database and working directories."""
    settings = get_settings()
    init_db()
    console.print(f"[green]Database ready[/green] at {settings.database_url}")


@app.command("google-login")
def google_login() -> None:
    """Run the Google OAuth consent flow and cache the token."""
    console.print("Opening a browser for Google sign-in...")
    try:
        get_credentials(allow_interactive=True)
    except GoogleAuthError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Signed in.[/green] Token saved to {get_settings().google_token_file}")


@app.command()
def backend() -> None:
    """Show the active scoring backend and check it actually responds."""
    from app.screening.backends import BackendError, OllamaBackend, get_backend

    settings = get_settings()
    console.print(f"Provider: [bold]{settings.screening_provider}[/bold]")

    try:
        active = get_backend(force_reload=True)
    except BackendError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"Backend:  {active.describe()}")

    if isinstance(active, OllamaBackend):
        installed = active.available_models()
        if not installed:
            console.print(
                f"[red]Ollama is not reachable at {active.base_url}.[/red] "
                "Open the Ollama app or run `ollama serve`."
            )
            raise typer.Exit(code=1)
        console.print("Installed: " + ", ".join(installed))
        if active.model not in installed:
            console.print(
                f"[yellow]{active.model!r} is not installed.[/yellow] "
                f"Run `ollama pull {active.model}` or set SCREENING_MODEL to one above."
            )
            raise typer.Exit(code=1)

    console.print("\nSending a test prompt...")
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    try:
        reply = active.complete_json(
            "You reply with JSON only.", "Return {\"ok\": true}.", schema
        )
    except BackendError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Backend is working.[/green] Reply: {reply.strip()[:80]}")


@app.command()
def fields() -> None:
    """List the canonical candidate fields."""
    table = Table(title="Canonical candidate fields")
    table.add_column("key", style="cyan")
    table.add_column("question title")
    table.add_column("type")
    table.add_column("required by default")
    for spec in FIELDS:
        table.add_row(spec.key, spec.title, spec.qtype, "yes" if spec.default_required else "no")
    console.print(table)


@app.command("new-job")
def new_job() -> None:
    """Interactive wizard: collect the full role spec from the admin."""
    console.print(Panel.fit("New job opening", style="bold cyan"))

    title = typer.prompt("Job title")
    department = typer.prompt("Department / team", default="", show_default=False) or None
    location = typer.prompt("Location", default="", show_default=False) or None
    employment_type = typer.prompt(
        "Employment type (full-time / contract / intern)", default="full-time"
    )
    description = typer.prompt("Short role description", default="", show_default=False) or None
    responsibilities = _csv_list(
        typer.prompt("Key responsibilities (comma-separated)", default="", show_default=False)
    )

    console.print("\n[bold]Requirements[/bold]")
    min_years = float(typer.prompt("Minimum years of experience", default="0"))
    max_years_raw = typer.prompt(
        "Maximum years of experience (blank for none)", default="", show_default=False
    )
    max_years = float(max_years_raw) if max_years_raw.strip() else None

    required_skills = _csv_list(
        typer.prompt("Must-have technologies/skills (comma-separated)")
    )
    preferred_skills = _csv_list(
        typer.prompt("Nice-to-have skills (comma-separated)", default="", show_default=False)
    )
    education = typer.prompt("Education requirement", default="", show_default=False) or None

    notice_raw = typer.prompt(
        "Maximum acceptable notice period in days (blank for none)", default="", show_default=False
    )
    max_notice = int(notice_raw) if notice_raw.strip() else None

    ctc_raw = typer.prompt(
        "Compensation ceiling, numbers only (blank for none)", default="", show_default=False
    )
    max_ctc = float(ctc_raw) if ctc_raw.strip() else None

    console.print("\n[bold]Screening[/bold]")
    console.print(
        "  lenient      — wide net, score cutoff 45\n"
        "  balanced     — default, score cutoff 62\n"
        "  strict       — cutoff 76, nearly all must-haves required\n"
        "  very_strict  — cutoff 87, every must-have required, no tolerance"
    )
    strictness = typer.prompt("Strictness", default=Strictness.balanced.value)
    if strictness not in {s.value for s in Strictness}:
        console.print(f"[red]Unknown strictness {strictness!r}.[/red]")
        raise typer.Exit(code=1)

    console.print(
        "\nMandatory questions (blank answers are auto-rejected). Default: "
        + ", ".join(DEFAULT_REQUIRED_FIELDS)
    )
    required_raw = typer.prompt(
        "Required field keys (comma-separated, blank for default)", default="", show_default=False
    )
    required_fields = _csv_list(required_raw) or list(DEFAULT_REQUIRED_FIELDS)
    unknown = [k for k in required_fields if k not in FIELDS_BY_KEY]
    if unknown:
        console.print(f"[red]Unknown field keys: {', '.join(unknown)}. Run `fields` to list them.[/red]")
        raise typer.Exit(code=1)

    screening_notes = (
        typer.prompt(
            "Anything else the screener should weigh? (free text)", default="", show_default=False
        )
        or None
    )

    with session_scope() as session:
        job = JobOpening(
            title=title,
            department=department,
            location=location,
            employment_type=employment_type,
            description=description,
            responsibilities=responsibilities,
            min_years_experience=min_years,
            max_years_experience=max_years,
            required_skills=required_skills,
            preferred_skills=preferred_skills,
            education_requirement=education,
            max_notice_period_days=max_notice,
            max_expected_ctc=max_ctc,
            strictness=strictness,
            required_fields=required_fields,
            screening_notes=screening_notes,
        )
        session.add(job)
        session.flush()
        job_id = job.id

    console.print(f"\n[green]Created job {job_id}: {title}[/green]")
    console.print(f"Next: [cyan]python -m app.cli create-form {job_id}[/cyan]")


@app.command("jobs")
def list_jobs() -> None:
    """List all job openings."""
    with session_scope() as session:
        jobs = session.scalars(select(JobOpening).order_by(JobOpening.id)).all()
        if not jobs:
            console.print("[dim]No jobs yet. Run `new-job`.[/dim]")
            return
        table = Table(title="Job openings")
        table.add_column("id", justify="right")
        table.add_column("title")
        table.add_column("strictness")
        table.add_column("status")
        table.add_column("form")
        table.add_column("candidates", justify="right")
        for job in jobs:
            table.add_row(
                str(job.id),
                job.title,
                job.strictness,
                job.status,
                job.form_url or "[dim]none[/dim]",
                str(len(job.candidates)),
            )
        console.print(table)


@app.command("show")
def show_job(job_id: int) -> None:
    """Print a job's full configuration."""
    with session_scope() as session:
        job = _load_job(session, job_id)
        console.print(Panel(job.spec_text(), title=f"Job {job.id}", style="cyan"))
        thresholds = job.thresholds
        console.print(
            f"Strictness [bold]{job.strictness}[/bold] -> "
            f"score cutoff {thresholds['score_cutoff']}, "
            f"must-have coverage {float(thresholds['must_have_ratio']):.0%}, "
            f"experience tolerance {thresholds['experience_slack']} years"
        )
        console.print("Mandatory fields: " + ", ".join(job.required_fields))
        if job.form_url:
            console.print(f"Form: {job.form_url}")
            console.print(f"Edit: {job.form_edit_url}")


@app.command("create-form")
def create_form(
    job_id: int,
    only: str = typer.Option(
        "", help="Comma-separated field keys to include, in order. Defaults to all."
    ),
) -> None:
    """Generate the Google Form for a job."""
    keys = _csv_list(only) or None
    with session_scope() as session:
        job = _load_job(session, job_id)
        try:
            info = forms_api.create_form_for_job(job, keys)
        except (GoogleAuthError, forms_api.FormError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

        job.form_id = info["form_id"]
        job.form_url = info["form_url"]
        job.form_edit_url = info["form_edit_url"]
        job.question_map = info["question_map"]
        job.status = JobStatus.open.value

    console.print("[green]Form created.[/green]")
    console.print(f"  Share:  {info['form_url']}")
    console.print(f"  Edit:   {info['form_edit_url']}")
    console.print(f"  Mapped {len(info['question_map'])} questions to canonical fields.")


@app.command("link-form")
def link_form(job_id: int, form_ref: str) -> None:
    """Attach an existing Google Form (URL or ID) and map its questions."""
    with session_scope() as session:
        job = _load_job(session, job_id)
        try:
            info = forms_api.inspect_form(forms_api.extract_form_id(form_ref))
        except (GoogleAuthError, forms_api.FormError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

        job.form_id = info["form_id"]
        job.form_url = info["form_url"]
        job.form_edit_url = info["form_edit_url"]
        job.question_map = info["question_map"]
        job.status = JobStatus.open.value

    console.print(f"[green]Linked form '{info['title']}'.[/green]")
    for key, qid in info["question_map"].items():
        console.print(f"  [cyan]{key}[/cyan] <- {qid}")
    if info["unmapped_questions"]:
        console.print("\n[yellow]Unrecognised questions (kept in raw_response only):[/yellow]")
        for item in info["unmapped_questions"]:
            console.print(f"  {item['title']}")


@app.command("send-link")
def send_link(
    job_id: int,
    to: list[str] = typer.Option(..., "--to", help="Recipient email. Repeat for multiple."),
    subject: str = typer.Option("", help="Override the subject line."),
    visible: bool = typer.Option(False, help="Put recipients in To: instead of Bcc:."),
) -> None:
    """Email the application-form link out."""
    with session_scope() as session:
        job = _load_job(session, job_id)
        try:
            result = send_form_link(job, list(to), subject=subject or None, bcc=not visible)
        except (GoogleAuthError, MailError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    console.print(f"[green]Sent to {result['recipients']} recipient(s).[/green]")


@app.command("sync")
def sync(job_id: int) -> None:
    """Pull form responses into the database."""
    with session_scope() as session:
        job = _load_job(session, job_id)
        try:
            result = sync_responses(session, job)
        except (GoogleAuthError, PipelineError, forms_api.FormError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    console.print(
        f"[green]Fetched {result.fetched}[/green] responses "
        f"({result.created} new, {result.updated} updated)."
    )
    if result.unmapped_answers:
        console.print(f"[yellow]{result.unmapped_answers} answers had no canonical field.[/yellow]")


@app.command("screen")
def screen(
    job_id: int,
    no_llm: bool = typer.Option(False, "--no-llm", help="Rule-based filtering only, no API calls."),
    rescreen: bool = typer.Option(False, help="Re-screen candidates that already have a verdict."),
) -> None:
    """Filter incomplete responses, apply hard rules, then score the survivors."""
    with session_scope() as session:
        job = _load_job(session, job_id)
        try:
            result = screen_job(session, job, use_llm=not no_llm, rescreen=rescreen)
        except ScoringError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

    table = Table(title=f"Screening results — job {job_id}")
    table.add_column("outcome")
    table.add_column("count", justify="right")
    table.add_row("[bold green]shortlisted[/bold green]", str(result.shortlisted))
    table.add_row("rejected — incomplete", str(result.rejected_incomplete))
    table.add_row("rejected — hard rules", str(result.rejected_rules))
    table.add_row("rejected — low fit score", str(result.rejected_score))
    table.add_row("[dim]screened total[/dim]", str(result.total))
    console.print(table)

    for error in result.errors:
        console.print(f"[yellow]{error}[/yellow]")


@app.command("run")
def run(
    job_id: int,
    no_llm: bool = typer.Option(False, "--no-llm", help="Rule-based filtering only."),
) -> None:
    """Sync then screen — the everyday command."""
    sync(job_id)
    screen(job_id, no_llm=no_llm, rescreen=False)


def _candidate_table(candidates: list[Candidate], title: str) -> Table:
    table = Table(title=title)
    table.add_column("id", justify="right")
    table.add_column("name")
    table.add_column("email")
    table.add_column("exp", justify="right")
    table.add_column("score", justify="right")
    table.add_column("rec")
    table.add_column("status")
    for c in candidates:
        table.add_row(
            str(c.id),
            c.full_name or "[dim]—[/dim]",
            c.email or "[dim]—[/dim]",
            f"{c.years_experience:g}" if c.years_experience is not None else "—",
            str(c.fit_score) if c.fit_score is not None else "—",
            c.recommendation or "—",
            f"[{STATUS_STYLE.get(c.status, '')}]{c.status}[/]",
        )
    return table


@app.command("candidates")
def list_candidates(
    job_id: int,
    status: str = typer.Option("", help="Filter by status, e.g. shortlisted."),
) -> None:
    """List every candidate for a job."""
    with session_scope() as session:
        _load_job(session, job_id)
        query = select(Candidate).where(Candidate.job_id == job_id)
        if status:
            query = query.where(Candidate.status == status)
        query = query.order_by(Candidate.fit_score.desc().nullslast(), Candidate.id)
        candidates = list(session.scalars(query).all())
        console.print(_candidate_table(candidates, f"Candidates — job {job_id}"))


@app.command("shortlist")
def shortlist(job_id: int) -> None:
    """Show the shortlist — the handoff point for Part 2."""
    with session_scope() as session:
        _load_job(session, job_id)
        candidates = list(
            session.scalars(
                select(Candidate)
                .where(
                    Candidate.job_id == job_id,
                    Candidate.status == CandidateStatus.shortlisted.value,
                )
                .order_by(Candidate.fit_score.desc().nullslast(), Candidate.id)
            ).all()
        )
        console.print(_candidate_table(candidates, f"Shortlist — job {job_id}"))


@app.command("candidate")
def show_candidate(candidate_id: int) -> None:
    """Full detail and screening rationale for one candidate."""
    with session_scope() as session:
        candidate = session.get(Candidate, candidate_id)
        if candidate is None:
            console.print(f"[red]Candidate {candidate_id} not found.[/red]")
            raise typer.Exit(code=1)

        console.print(Panel(candidate.profile_text(), title=candidate.full_name or "Candidate"))
        console.print(f"Status: [{STATUS_STYLE.get(candidate.status, '')}]{candidate.status}[/]")
        if candidate.fit_score is not None:
            console.print(f"Fit score: [bold]{candidate.fit_score}[/bold]  ({candidate.recommendation})")
        if candidate.missing_fields:
            console.print("[yellow]Missing fields:[/yellow] " + ", ".join(candidate.missing_fields))
        for failure in candidate.rule_failures:
            console.print(f"[red]Rule:[/red] {failure}")
        if candidate.rationale:
            console.print(Panel(candidate.rationale, title="Rationale"))
        if candidate.assessment:
            for label in ("strengths", "concerns"):
                items = candidate.assessment.get(label) or []
                if items:
                    console.print(f"[bold]{label.title()}[/bold]")
                    for item in items:
                        console.print(f"  • {item}")


@app.command("export")
def export(
    job_id: int,
    out: Path = typer.Option(None, help="Output CSV path."),
    shortlist_only: bool = typer.Option(True, help="Export only shortlisted candidates."),
) -> None:
    """Export candidates to CSV."""
    target = out or EXPORT_DIR / f"job_{job_id}_candidates.csv"
    target.parent.mkdir(parents=True, exist_ok=True)

    with session_scope() as session:
        _load_job(session, job_id)
        query = select(Candidate).where(Candidate.job_id == job_id)
        if shortlist_only:
            query = query.where(Candidate.status == CandidateStatus.shortlisted.value)
        query = query.order_by(Candidate.fit_score.desc().nullslast(), Candidate.id)
        candidates = list(session.scalars(query).all())

        columns = [
            "id", "full_name", "email", "phone", "years_experience", "current_role",
            "current_company", "skills", "education", "location", "notice_period_days",
            "expected_ctc", "linkedin", "resume_url", "portfolio_url", "status",
            "fit_score", "recommendation", "rationale",
        ]
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for c in candidates:
                writer.writerow(
                    [
                        json.dumps(getattr(c, col)) if col == "skills" else getattr(c, col)
                        for col in columns
                    ]
                )

    console.print(f"[green]Wrote {len(candidates)} row(s)[/green] to {target}")


def _load_round(session, round_id: int):
    from app.models import InterviewRound

    round_ = session.get(InterviewRound, round_id)
    if round_ is None:
        console.print(f"[red]Round {round_id} not found.[/red]")
        raise typer.Exit(code=1)
    return round_


def _schedule_table(slots, title: str) -> Table:
    table = Table(title=title)
    table.add_column("candidate")
    table.add_column("score", justify="right")
    table.add_column("when")
    table.add_column("email")
    table.add_column("phone")
    table.add_column("status")
    for slot in slots:
        c = slot.candidate
        table.add_row(
            c.full_name or f"#{c.id}",
            str(c.fit_score) if c.fit_score is not None else "—",
            f"{slot.local_start():%a %d %b %H:%M}–{slot.local_end():%H:%M}",
            c.email or "[red]missing[/red]",
            c.phone or "[red]missing[/red]",
            slot.status,
        )
    return table


@app.command("round-new")
def round_new(job_id: int) -> None:
    """Create an interview round: acceptable score, date, and slot settings."""
    import datetime as date_mod

    from app.models import InterviewMode, InterviewRound

    settings = get_settings()
    with session_scope() as session:
        job = _load_job(session, job_id)
        console.print(Panel.fit(f"New interview round — {job.title}", style="bold cyan"))

        name = typer.prompt("Round name", default="Technical Round 1")
        acceptable = int(
            typer.prompt(
                "Acceptable score (candidates below this are rejected)",
                default=str(int(job.thresholds["score_cutoff"])),
            )
        )
        if acceptable < job.thresholds["score_cutoff"]:
            console.print(
                f"[yellow]Note:[/yellow] Part 1 already filtered below "
                f"{int(job.thresholds['score_cutoff'])}, so a bar of {acceptable} rejects no one."
            )

        date_raw = typer.prompt("First interview date (YYYY-MM-DD)")
        try:
            start_date = date_mod.date.fromisoformat(date_raw)
        except ValueError:
            console.print("[red]Date must be YYYY-MM-DD.[/red]")
            raise typer.Exit(code=1) from None

        def _time(label: str, default: str) -> date_mod.time:
            raw = typer.prompt(label, default=default)
            try:
                return date_mod.time.fromisoformat(raw)
            except ValueError:
                console.print(f"[red]{raw!r} is not a valid time (use HH:MM).[/red]")
                raise typer.Exit(code=1) from None

        day_start = _time("Day starts at", "10:00")
        day_end = _time("Day ends at", "17:00")
        slot_minutes = int(typer.prompt("Slot length in minutes", default="30"))
        break_minutes = int(typer.prompt("Break between slots in minutes", default="0"))
        skip_weekends = typer.confirm("Skip weekends?", default=True)
        timezone = typer.prompt("Timezone (IANA)", default=settings.default_timezone)

        mode = typer.prompt("Mode (online / onsite / phone)", default="online")
        if mode not in {m.value for m in InterviewMode}:
            console.print(f"[red]Unknown mode {mode!r}.[/red]")
            raise typer.Exit(code=1)
        location = typer.prompt(
            "Meeting link" if mode == "online" else "Address",
            default="",
            show_default=False,
        ) or None
        instructions = typer.prompt("Extra instructions", default="", show_default=False) or None
        contact = typer.prompt("Reply-to contact email", default="", show_default=False) or None

        round_ = InterviewRound(
            job_id=job.id, name=name, acceptable_score=acceptable, start_date=start_date,
            day_start_time=day_start, day_end_time=day_end, slot_minutes=slot_minutes,
            break_minutes=break_minutes, skip_weekends=skip_weekends, timezone=timezone,
            mode=mode, location=location, instructions=instructions, contact_email=contact,
        )
        session.add(round_)
        session.flush()
        round_id = round_.id

    console.print(f"\n[green]Created round {round_id}: {name}[/green]")
    console.print(f"Next: [cyan]python -m app.cli schedule {round_id}[/cyan]")


@app.command("rounds")
def list_rounds(job_id: int) -> None:
    """List interview rounds for a job."""
    from app.models import InterviewRound

    with session_scope() as session:
        _load_job(session, job_id)
        rounds = session.scalars(
            select(InterviewRound).where(InterviewRound.job_id == job_id).order_by(InterviewRound.id)
        ).all()
        if not rounds:
            console.print("[dim]No rounds yet. Run `round-new`.[/dim]")
            return
        table = Table(title=f"Interview rounds — job {job_id}")
        table.add_column("id", justify="right")
        table.add_column("name")
        table.add_column("bar", justify="right")
        table.add_column("from")
        table.add_column("slots", justify="right")
        table.add_column("status")
        for r in rounds:
            table.add_row(
                str(r.id), r.name, str(r.acceptable_score), r.start_date.isoformat(),
                str(len([s for s in r.slots if s.status != "cancelled"])), r.status,
            )
        console.print(table)


@app.command("schedule")
def schedule(
    round_id: int,
    apply: bool = typer.Option(False, "--apply", help="Persist. Without this it only previews."),
) -> None:
    """Apply the acceptable score and assign slots. Previews unless --apply is given."""
    from app.notify.rounds import allocate_round, apply_score_bar, commit_score_bar
    from app.scheduling import SchedulingError, SlotWindow, describe_window

    with session_scope() as session:
        round_ = _load_round(session, round_id)
        bar = apply_score_bar(session, round_)

        console.print(
            f"[bold]{round_.name}[/bold] — acceptable score {round_.acceptable_score}\n"
            f"{len(bar.invited)} invited, {len(bar.rejected)} rejected"
            + (f", {len(bar.unscored)} unscored" if bar.unscored else "")
        )

        if bar.rejected:
            console.print("\n[red]Below the bar (will be marked rejected, not messaged):[/red]")
            for c in bar.rejected:
                console.print(f"  {c.full_name or c.id} — {c.fit_score}")

        if bar.unscored:
            console.print(
                "\n[yellow]No fit score (screened with --no-llm?) — held out, decide manually:[/yellow]"
            )
            for c in bar.unscored:
                console.print(f"  {c.full_name or c.id}")

        if not bar.invited:
            console.print("\n[yellow]Nobody clears the bar. Lower it or re-screen.[/yellow]")
            raise typer.Exit(code=1)

        window = SlotWindow(
            start_date=round_.start_date, day_start_time=round_.day_start_time,
            day_end_time=round_.day_end_time, slot_minutes=round_.slot_minutes,
            break_minutes=round_.break_minutes, skip_weekends=round_.skip_weekends,
            timezone=round_.timezone,
        )
        try:
            console.print("\n" + describe_window(window, len(bar.invited)))
        except SchedulingError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

        if not apply:
            console.print(
                "\n[yellow]Preview only.[/yellow] Re-run with [cyan]--apply[/cyan] to save."
            )
            return

        commit_score_bar(session, round_, bar)
        slots = allocate_round(session, round_, bar.invited)
        console.print()
        console.print(_schedule_table(slots, f"Scheduled — {round_.name}"))
        console.print(f"\nNext: [cyan]python -m app.cli notify {round_id}[/cyan]")


@app.command("round-show")
def round_show(round_id: int) -> None:
    """Show a round's schedule and delivery status."""
    with session_scope() as session:
        round_ = _load_round(session, round_id)
        console.print(
            Panel(
                f"{round_.name}\n"
                f"Acceptable score: {round_.acceptable_score}\n"
                f"From {round_.start_date}, {round_.day_start_time}–{round_.day_end_time} "
                f"{round_.timezone}, {round_.slot_minutes} min slots\n"
                f"Mode: {round_.mode}" + (f" — {round_.location}" if round_.location else ""),
                title=f"Round {round_.id}",
                style="cyan",
            )
        )
        active = [s for s in round_.slots if s.status != "cancelled"]
        if not active:
            console.print("[dim]No slots yet. Run `schedule`.[/dim]")
            return
        console.print(_schedule_table(active, "Schedule"))
        for slot in active:
            for channel, error in (slot.notify_errors or {}).items():
                console.print(f"[red]{slot.candidate.full_name} [{channel}]:[/red] {error}")


@app.command("notify")
def notify(
    round_id: int,
    no_email: bool = typer.Option(False, "--no-email", help="Skip the email channel."),
    dry_run: bool = typer.Option(
        None, "--dry-run/--send", help="Override NOTIFY_DRY_RUN for this run."
    ),
    retry_failed: bool = typer.Option(
        False, help="Also re-send to candidates already notified successfully."
    ),
) -> None:
    """Send each scheduled candidate their interview details by email."""
    from app.notify.channels import ChannelError, build_channels
    from app.notify.rounds import notify_round

    with session_scope() as session:
        round_ = _load_round(session, round_id)
        active = [s for s in round_.slots if s.status != "cancelled"]
        if not active:
            console.print("[yellow]No slots to notify. Run `schedule --apply` first.[/yellow]")
            raise typer.Exit(code=1)

        try:
            channels = build_channels(use_email=not no_email, dry_run=dry_run)
        except ChannelError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

        for channel in channels:
            console.print(f"  channel: {channel.describe()}")

        live = not any("DRY RUN" in c.describe() for c in channels)
        if live:
            console.print(
                f"\n[bold yellow]This will message {len(active)} real candidate(s).[/bold yellow]"
            )
            if not typer.confirm("Send for real?", default=False):
                console.print("Aborted.")
                raise typer.Exit(code=1)

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
        except Exception as exc:
            console.print(f"[dim]No interview links attached ({exc}).[/dim]")

        if join_urls:
            console.print(f"  attaching {len(join_urls)} personal interview link(s)")

        result = notify_round(
            session, round_, channels,
            only_pending=not retry_failed, join_urls=join_urls, dry_run=not live,
        )

    console.print(
        f"\n[green]sent {result.sent}[/green], partial {result.partial}, "
        f"failed {result.failed}, skipped {result.skipped}"
    )
    for error in result.errors:
        console.print(f"[red]{error}[/red]")
    if not live:
        console.print("\n[yellow]Dry run — nothing was actually sent.[/yellow]")


@app.command("interview-prepare")
def interview_prepare(
    round_id: int,
    difficulty: str = typer.Option(
        "", help="Override the round's difficulty: easy | medium | hard | expert."
    ),
    no_plan: bool = typer.Option(False, "--no-plan", help="Skip question generation (no API calls)."),
    no_resume: bool = typer.Option(False, "--no-resume", help="Skip fetching resume documents."),
) -> None:
    """Create an AI interview for every scheduled candidate and print their join links."""
    from app.interview.db import init_interview_db, interview_session_scope
    from app.interview.models import Difficulty
    from app.interview.service import InterviewError, join_url, prepare_interviews

    init_interview_db()

    with session_scope() as main, interview_session_scope() as interviews:
        round_ = _load_round(main, round_id)

        if difficulty:
            if difficulty not in {d.value for d in Difficulty}:
                console.print(f"[red]Unknown difficulty {difficulty!r}.[/red]")
                raise typer.Exit(code=1)
            round_.difficulty = difficulty
            main.commit()

        console.print(
            f"Preparing [bold]{round_.name}[/bold] at difficulty "
            f"[bold]{round_.difficulty}[/bold]..."
        )
        try:
            prepared = prepare_interviews(
                main,
                interviews,
                round_,
                plan_questions=not no_plan,
                fetch_resumes=not no_resume,
            )
        except InterviewError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

        if not prepared:
            console.print("[yellow]No scheduled candidates. Run `schedule --apply` first.[/yellow]")
            raise typer.Exit(code=1)

        table = Table(title=f"Interviews prepared — {round_.name}")
        table.add_column("candidate")
        table.add_column("questions", justify="right")
        table.add_column("resume")
        table.add_column("join link")
        for interview in prepared:
            plan = interview.question_plan or {}
            count = len(plan.get("questions", [])) if isinstance(plan, dict) else len(plan)
            table.add_row(
                interview.candidate_name or f"#{interview.candidate_id}",
                str(count),
                "fetched" if interview.resume_text else "[dim]profile only[/dim]",
                join_url(interview),
            )
        console.print(table)

    console.print(
        f"\nSend the links with [cyan]python -m app.cli notify {round_id} --send[/cyan] "
        "(each candidate gets their own)."
    )


@app.command("interviews")
def list_interviews(round_id: int) -> None:
    """List interviews for a round with their status and ratings."""
    from app.interview.db import init_interview_db, interview_session_scope
    from app.interview.models import Interview

    init_interview_db()
    with interview_session_scope() as interviews:
        rows = interviews.scalars(
            select(Interview)
            .where(Interview.round_id == round_id)
            .order_by(Interview.scheduled_start, Interview.id)
        ).all()
        if not rows:
            console.print("[dim]None yet. Run `interview-prepare`.[/dim]")
            return

        table = Table(title=f"Interviews — round {round_id}")
        table.add_column("id", justify="right")
        table.add_column("candidate")
        table.add_column("status")
        table.add_column("rating", justify="right")
        table.add_column("verdict")
        table.add_column("flags", justify="right")
        table.add_column("turns", justify="right")
        for row in rows:
            style = {
                "graded": "bold green", "completed": "green",
                "terminated": "red", "in_progress": "yellow", "no_show": "red",
            }.get(row.status, "dim")
            table.add_row(
                str(row.id),
                row.candidate_name or f"#{row.candidate_id}",
                f"[{style}]{row.status}[/]",
                str(row.overall_rating) if row.overall_rating is not None else "—",
                row.recommendation or "—",
                str(row.violation_count or 0),
                str(len(row.turns)),
            )
        console.print(table)


@app.command("interview-show")
def interview_show(
    interview_id: int,
    transcript: bool = typer.Option(True, help="Include the full transcript."),
) -> None:
    """Show one interview: plan, transcript, proctoring events, and the report."""
    from app.interview.db import init_interview_db, interview_session_scope
    from app.interview.models import Interview
    from app.interview.service import join_url

    init_interview_db()
    with interview_session_scope() as interviews:
        row = interviews.get(Interview, interview_id)
        if row is None:
            console.print(f"[red]Interview {interview_id} not found.[/red]")
            raise typer.Exit(code=1)

        duration = row.duration_seconds()
        console.print(
            Panel(
                f"{row.candidate_name or row.candidate_id}  (candidate {row.candidate_id})\n"
                f"Status: {row.status}   Difficulty: {row.difficulty}\n"
                f"Duration: {duration / 60:.1f} min" if duration else "Duration: —",
                title=f"Interview {row.id}",
                style="cyan",
            )
        )
        console.print(f"Join link: {join_url(row)}")

        if transcript and row.turns:
            console.print()
            console.print("[bold]Transcript[/bold]")
            for turn in row.turns:
                who = "Interviewer" if turn.speaker == "interviewer" else "Candidate"
                colour = "cyan" if turn.speaker == "interviewer" else "white"
                stamp = f"{int(turn.at_seconds // 60):02d}:{int(turn.at_seconds % 60):02d}"
                console.print(f"[dim]{stamp}[/dim] [{colour}]{who}:[/] {turn.text}")

        counted = [e for e in row.events if e.counted]
        if counted:
            console.print()
            console.print("[bold yellow]Proctoring violations[/bold yellow]")
            for event in counted:
                stamp = f"{int(event.at_seconds // 60):02d}:{int(event.at_seconds % 60):02d}"
                console.print(f"  [{stamp}] {event.kind} for {event.duration_seconds:.1f}s")

        if row.summary:
            console.print()
            console.print(
                Panel(
                    row.summary,
                    title=f"Rating {row.overall_rating}/100 — {row.recommendation}",
                    style="green",
                )
            )
            ratings = row.ratings or {}
            if ratings.get("competencies"):
                table = Table(title="Competencies")
                table.add_column("competency")
                table.add_column("level")
                table.add_column("score", justify="right")
                for item in ratings["competencies"]:
                    table.add_row(item["competency"], item["level"], str(item["score"]))
                console.print(table)
            for label, colour in (("strengths", "green"), ("concerns", "yellow"), ("red_flags", "red")):
                items = ratings.get(label) or []
                if items:
                    console.print(f"[bold {colour}]{label.replace('_', ' ').title()}[/bold {colour}]")
                    for item in items:
                        console.print(f"  • {item}")
        elif row.turns:
            console.print(
                f"\n[dim]Not graded yet — run "
                f"`python -m app.cli interview-grade {row.id}`.[/dim]"
            )


@app.command("interview-grade")
def interview_grade(
    round_id: int = typer.Option(None, help="Grade every ungraded interview in a round."),
    interview_id: int = typer.Argument(None, help="Or grade a single interview."),
) -> None:
    """Write the summary and ratings from a stored transcript."""
    from app.interview.db import init_interview_db, interview_session_scope
    from app.interview.models import Interview, InterviewStatus
    from app.interview.service import InterviewError, grade_and_store

    if not interview_id and not round_id:
        console.print("[red]Give an interview id, or --round-id to grade a whole round.[/red]")
        raise typer.Exit(code=1)

    init_interview_db()
    with interview_session_scope() as interviews:
        if interview_id:
            rows = [interviews.get(Interview, interview_id)]
            if rows[0] is None:
                console.print(f"[red]Interview {interview_id} not found.[/red]")
                raise typer.Exit(code=1)
        else:
            rows = list(
                interviews.scalars(
                    select(Interview).where(
                        Interview.round_id == round_id,
                        Interview.status != InterviewStatus.graded.value,
                    )
                ).all()
            )
            if not rows:
                console.print("[yellow]Nothing to grade.[/yellow]")
                return

        for row in rows:
            label = row.candidate_name or f"#{row.candidate_id}"
            try:
                grade_and_store(interviews, row)
            except InterviewError as exc:
                console.print(f"[yellow]{label}: {exc}[/yellow]")
                continue
            except Exception as exc:
                console.print(f"[red]{label}: grading failed — {exc}[/red]")
                continue
            graded_by = (row.providers or {}).get("grading")
            suffix = f" [dim](graded by {graded_by})[/dim]" if graded_by else ""
            console.print(
                f"[green]{label}[/green]: {row.overall_rating}/100 — "
                f"{row.recommendation}{suffix}"
            )


@app.command("interview-backend")
def interview_backend(
    probe: bool = typer.Option(
        False, "--probe", help="Actually call each provider instead of only reading config."
    ),
) -> None:
    """Show which provider will run Part 3, and what happens if it goes down."""
    from app.interview.providers import PROVIDERS, configured_providers
    from app.interview.realtime import live_provider_order

    settings = get_settings()
    text_order = configured_providers()
    live_order = live_provider_order()

    table = Table(title="Part 3 providers")
    table.add_column("stage", style="cyan")
    table.add_column("primary")
    table.add_column("fallback")
    table.add_column("model")

    def _row(stage: str, order: list[str], model: str) -> None:
        table.add_row(
            stage,
            order[0] if order else "[red]none[/red]",
            order[1] if len(order) > 1 else "[yellow]none[/yellow]",
            model,
        )

    primary_text = text_order[0] if text_order else None
    text_model = (
        settings.grading_model if primary_text == "openai"
        else settings.gemini_model if primary_text == "gemini"
        else "-"
    )
    live_model = (
        settings.realtime_model if live_order and live_order[0] == "openai"
        else settings.gemini_live_model if live_order else "-"
    )
    _row("question plan", text_order, text_model)
    _row("live voice call", live_order, live_model)
    _row("grading", text_order, text_model)
    console.print(table)

    if not text_order or not live_order:
        console.print(
            "\n[red]No provider is configured.[/red] Set OPENAI_API_KEY or GEMINI_API_KEY "
            "in .env — Part 3 cannot run without one."
        )
        raise typer.Exit(code=1)

    if len(live_order) < 2:
        console.print(
            f"\n[yellow]No fallback.[/yellow] Only {live_order[0]} is configured, so an "
            "outage there means interviews cannot run. Add the other key to .env:\n"
            "  OPENAI_API_KEY=...   https://platform.openai.com/api-keys\n"
            "  GEMINI_API_KEY=...   https://aistudio.google.com/apikey"
        )
    else:
        console.print(
            f"\n[green]Failover is set up.[/green] If {live_order[0]} fails, interviews "
            f"switch to {live_order[1]} automatically."
        )

    if not probe:
        console.print("\n[dim]Config only. Re-run with --probe to actually call each one.[/dim]")
        return

    console.print("\n[bold]Text path[/bold] (question plan + grading)")
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    for name in text_order:
        provider = PROVIDERS[name]
        try:
            reply = provider.complete_json(
                "You reply with JSON only.", 'Return {"ok": true}.', "probe", schema
            )
        except Exception as exc:
            console.print(f"  [red]FAIL[/red] {name}: {str(exc)[:200]}")
            continue
        console.print(f"  [green]ok[/green]   {name} ({provider.describe()}): {reply.strip()[:60]}")

    console.print("\n[bold]Voice path[/bold] (the live call itself)")
    for name in live_order:
        if name == "openai":
            from app.interview.realtime import RealtimeError, mint_client_secret

            probe_interview = SimpleNamespace(
                difficulty="medium", question_plan={}, resume_snapshot={},
                resume_text=None, candidate_name="Probe",
            )
            try:
                minted = mint_client_secret(probe_interview, timeout=20)
            except RealtimeError as exc:
                console.print(f"  [red]FAIL[/red] openai: {str(exc)[:200]}")
                continue
            except Exception as exc:  # noqa: BLE001 - report anything, never crash the probe
                console.print(f"  [red]FAIL[/red] openai: {type(exc).__name__}: {str(exc)[:170]}")
                continue
            secret = minted["client_secret"]
            console.print(
                f"  [green]ok[/green]   openai realtime ({minted['model']}): "
                f"minted {secret[:6]}…"
            )
        else:
            ok_live, detail = _probe_gemini_live()
            style = "[green]ok[/green]  " if ok_live else "[red]FAIL[/red]"
            console.print(f"  {style} gemini live: {detail}")


def _probe_gemini_live() -> tuple[bool, str]:
    """Open a real Gemini Live socket, send our setup frame, wait for setupComplete.

    This is the only thing that actually proves the voice fallback works. It is cheap —
    the handshake alone, no audio — but it is a genuine connection, so it catches a wrong
    model name, a key without Live access, and a blocked WebSocket.
    """
    import asyncio
    import json as _json

    from app.interview.realtime import (
        RealtimeError,
        gemini_live_url,
        gemini_setup_message,
    )

    settings = get_settings()
    probe_interview = SimpleNamespace(
        difficulty="medium", question_plan={}, resume_snapshot={},
        resume_text=None, candidate_name="Probe",
    )

    async def handshake() -> tuple[bool, str]:
        import websockets

        try:
            url = gemini_live_url()
        except RealtimeError as exc:
            return False, str(exc)
        try:
            async with websockets.connect(url, max_size=None, open_timeout=20) as ws:
                await ws.send(_json.dumps(gemini_setup_message(probe_interview)))
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                frame = _json.loads(raw)
                if "setupComplete" in frame:
                    return True, f"{settings.gemini_live_model} accepted the session"
                return False, f"unexpected first frame: {str(frame)[:150]}"
        except asyncio.TimeoutError:
            return False, "timed out waiting for setupComplete"
        except Exception as exc:  # noqa: BLE001 - never leak the URL, it holds the key
            return False, f"{type(exc).__name__}: {str(exc)[:150]}"

    try:
        return asyncio.run(handshake())
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:150]}"


@app.command("serve")
def serve(
    host: str = typer.Option("", help="Override API_HOST."),
    port: int = typer.Option(0, help="Override API_PORT."),
    reload: bool = typer.Option(False, help="Auto-reload on code changes."),
    public_url: str = typer.Option(
        "",
        "--public-url",
        help="Public https base URL candidates reach, e.g. a tunnel URL. Overrides "
             "INTERVIEW_BASE_URL for this run.",
    ),
) -> None:
    """Start the FastAPI server (API, admin dashboard, and candidate call pages)."""
    import os
    import socket

    import uvicorn

    if public_url:
        cleaned = public_url.rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            cleaned = "https://" + cleaned
        if cleaned.startswith("http://") and "localhost" not in cleaned and "127.0.0.1" not in cleaned:
            console.print(
                f"[red]{cleaned} is not https.[/red]\n"
                "Browsers refuse camera and microphone access on a remote http origin, so "
                "the interview page would load but the call could never start."
            )
            raise typer.Exit(code=1)
        os.environ["INTERVIEW_BASE_URL"] = cleaned
        get_settings.cache_clear()

    settings = get_settings()
    bind_host = host or settings.api_host
    bind_port = port or settings.api_port

    public = not any(
        h in settings.interview_base_url for h in ("127.0.0.1", "localhost", "0.0.0.0")
    )
    if public and not settings.admin_password:
        console.print(
            "[bold red]Refusing to start: the server is publicly reachable but the admin "
            "dashboard has no password.[/bold red]\n\n"
            f"INTERVIEW_BASE_URL is {settings.interview_base_url}, so anyone with that URL "
            "could open /admin and read every candidate's name, email, phone, resume link "
            "and interview transcript — and send real invitations.\n\n"
            "Set a password in .env:\n"
            "  ADMIN_PASSWORD=something-long-and-random\n\n"
            "[dim]Candidate interview links keep working without it — they are authorised by "
            "the secret token in each personal link.[/dim]"
        )
        raise typer.Exit(code=1)

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((bind_host, bind_port))
    except OSError:
        console.print(
            f"[red]Port {bind_port} is already in use on {bind_host}.[/red]\n\n"
            "Something else is already listening — most often an earlier copy of this "
            "server that was never stopped.\n\n"
            "[bold]Find it:[/bold]\n"
            f"  Windows      netstat -ano | findstr :{bind_port}\n"
            "               taskkill /PID <pid> /F\n"
            f"  macOS/Linux  lsof -i :{bind_port}\n"
            "               kill <pid>\n\n"
            "[bold]Or just use a different port:[/bold]\n"
            f"  python -m app.cli serve --port {bind_port + 1}\n\n"
            "[dim]If you change the port, update INTERVIEW_BASE_URL in .env too, or the "
            "candidate interview links will point at the wrong place.[/dim]"
        )
        raise typer.Exit(code=1) from None
    finally:
        probe.close()

    console.print(f"[green]Serving on http://{bind_host}:{bind_port}[/green]")
    console.print(f"  dashboard      http://{bind_host}:{bind_port}/admin")
    console.print(f"  API docs       http://{bind_host}:{bind_port}/docs")
    console.print(f"  candidate links {settings.interview_base_url}/interview/<token>")
    if settings.admin_password:
        console.print(f"  [dim]admin login as '{settings.admin_username}'[/dim]")
    if public:
        console.print(
            "\n[yellow]Public mode.[/yellow] Interview links point at "
            f"{settings.interview_base_url} — keep the tunnel running until every "
            "interview is done, or those links stop working."
        )

    uvicorn.run("app.main:app", host=bind_host, port=bind_port, reload=reload)


if __name__ == "__main__":
    app()
