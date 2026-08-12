"""Component health checks for the dashboard.

Every check is **non-destructive and non-interactive**. In particular the Google check must
never trigger the OAuth consent flow: a status page that pops a browser window on an
unattended server is worse than no status page at all, so it inspects the cached token
rather than calling ``get_credentials()``.

Checks come in two depths. The default is configuration-only and instant. ``probe=True``
additionally makes live calls (Ollama tags, OpenAI models, a Google token refresh), which is
what you want before running a real round but too slow to poll.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from app.config import get_settings

State = Literal["ok", "warn", "error", "off"]


@dataclass
class Check:
    name: str
    state: State
    detail: str
    fix: str | None = None
    meta: dict = field(default_factory=dict)


def _database() -> Check:
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import Candidate, InterviewRound, JobOpening

    try:
        with SessionLocal() as session:
            jobs = session.scalar(select(func.count(JobOpening.id))) or 0
            candidates = session.scalar(select(func.count(Candidate.id))) or 0
            rounds = session.scalar(select(func.count(InterviewRound.id))) or 0
    except Exception as exc:
        return Check("Candidate database", "error", str(exc)[:200], "Run `python -m app.cli init`.")

    return Check(
        "Candidate database",
        "ok",
        f"{jobs} job(s), {candidates} candidate(s), {rounds} round(s)",
        meta={"jobs": jobs, "candidates": candidates, "rounds": rounds},
    )


def _interview_database() -> Check:
    from sqlalchemy import func, select

    from app.interview.db import InterviewSession
    from app.interview.models import Interview

    try:
        with InterviewSession() as session:
            total = session.scalar(select(func.count(Interview.id))) or 0
            graded = (
                session.scalar(
                    select(func.count(Interview.id)).where(Interview.status == "graded")
                )
                or 0
            )
    except Exception as exc:
        return Check("Interview database", "error", str(exc)[:200], "Run `python -m app.cli init`.")

    return Check(
        "Interview database",
        "ok",
        f"{total} interview(s), {graded} graded — stored separately from candidate data",
        meta={"interviews": total, "graded": graded},
    )


def _screening_backend(probe: bool) -> Check:
    settings = get_settings()
    provider = settings.screening_provider.lower()

    if provider == "ollama":
        model = settings.screening_model or "gemma4:latest"
        if not probe:
            return Check("Screening model", "ok", f"ollama · {model} (not probed)")

        import httpx

        try:
            response = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5)
            response.raise_for_status()
            installed = [m["name"] for m in response.json().get("models", [])]
        except Exception:
            return Check(
                "Screening model",
                "error",
                f"Ollama is not reachable at {settings.ollama_base_url}",
                "Start the Ollama app, or run `ollama serve`.",
            )

        if model not in installed:
            return Check(
                "Screening model",
                "error",
                f"{model!r} is not installed. Available: {', '.join(installed) or 'none'}",
                f"Run `ollama pull {model}`, or change SCREENING_MODEL.",
                meta={"installed": installed},
            )
        return Check(
            "Screening model",
            "ok",
            f"ollama · {model} · {len(installed)} model(s) installed",
            meta={"installed": installed},
        )

    if not settings.openai_api_key:
        return Check(
            "Screening model",
            "error",
            "SCREENING_PROVIDER=openai but OPENAI_API_KEY is not set",
            "Add the key to .env, or set SCREENING_PROVIDER=ollama.",
        )
    return Check("Screening model", "ok", f"openai · {settings.screening_model or 'gpt-5'}")


def _openai(probe: bool) -> Check:
    """OpenAI powers Part 3 regardless of which screening backend is chosen.

    A missing key is only fatal when Gemini is not configured either — with a fallback in
    place Part 3 still runs, so reporting "error" would be wrong.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        has_fallback = bool(settings.gemini_api_key)
        return Check(
            "OpenAI (voice interview)",
            "off" if has_fallback else "error",
            "OPENAI_API_KEY is not set"
            + (" — Part 3 will run on Gemini" if has_fallback else " — Part 3 cannot run"),
            "Add OPENAI_API_KEY to .env." if not has_fallback
            else "Add OPENAI_API_KEY to .env if you want OpenAI as well.",
        )

    if not probe:
        return Check(
            "OpenAI (voice interview)",
            "ok",
            f"key set · realtime {settings.realtime_model} · grading {settings.grading_model} "
            "(not probed)",
        )

    try:
        import openai

        client = openai.OpenAI(
            api_key=settings.openai_api_key, base_url=settings.openai_base_url or None
        )
        available = {m.id for m in client.models.list()}
    except Exception as exc:
        return Check(
            "OpenAI (voice interview)",
            "error",
            f"Key rejected or unreachable: {str(exc)[:160]}"
            + (" — interviews will fall back to Gemini" if settings.gemini_api_key else ""),
            "Check OPENAI_API_KEY. If it was ever shared, rotate it.",
        )

    missing = [
        name
        for name in (settings.realtime_model, settings.grading_model)
        if name not in available
    ]
    if missing:
        return Check(
            "OpenAI (voice interview)",
            "warn",
            f"Key works, but this account cannot see: {', '.join(missing)}",
            "Pick models your account has access to in .env.",
            meta={"missing": missing},
        )
    return Check(
        "OpenAI (voice interview)",
        "ok",
        f"key works · realtime {settings.realtime_model} · grading {settings.grading_model}",
    )


def _gemini(probe: bool) -> Check:
    """The automatic fallback for Part 3 — planning, the live call, and grading."""
    settings = get_settings()
    primary = (settings.interview_provider or "openai").strip().lower()
    role = "primary" if primary == "gemini" else "fallback"

    if not settings.gemini_api_key:
        return Check(
            "Gemini (interview fallback)",
            "off",
            "GEMINI_API_KEY is not set — Part 3 depends entirely on OpenAI",
            "Add GEMINI_API_KEY to .env so interviews survive an OpenAI outage. "
            "Get one at https://aistudio.google.com/apikey",
        )

    detail = f"{role} · text {settings.gemini_model} · live {settings.gemini_live_model}"

    if not probe:
        return Check("Gemini (interview fallback)", "ok", f"key set · {detail} (not probed)")

    url = (
        f"{settings.gemini_base_url.rstrip('/')}/v1beta/models/"
        f"{settings.gemini_model}:generateContent"
    )
    try:
        response = httpx.post(
            url,
            headers={"x-goog-api-key": settings.gemini_api_key},
            json={
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "generationConfig": {"maxOutputTokens": 1},
            },
            timeout=30,
        )
    except httpx.HTTPError as exc:
        return Check(
            "Gemini (interview fallback)",
            "error",
            f"Unreachable: {str(exc)[:160]}",
            "Check the network and GEMINI_BASE_URL.",
        )

    if response.status_code >= 400:
        message = response.text[:200]
        try:
            message = response.json().get("error", {}).get("message", message)[:200]
        except ValueError:
            pass

        if response.status_code in (401, 403):
            fix = "Check GEMINI_API_KEY at https://aistudio.google.com/apikey"
        elif response.status_code == 404:
            fix = (
                f"GEMINI_MODEL={settings.gemini_model} is not usable by this key — Google "
                "retires models for new keys while still listing them. Set GEMINI_MODEL to "
                "a current one (gemini-flash-latest tracks the newest stable Flash)."
            )
        elif response.status_code == 429:
            fix = "Quota exhausted. Wait, or raise the limit in Google AI Studio."
        else:
            fix = "See the message above."

        return Check(
            "Gemini (interview fallback)",
            "error",
            f"{settings.gemini_model} rejected the request ({response.status_code}): {message}",
            fix,
            meta={"model": settings.gemini_model, "status": response.status_code},
        )

    live_note = ""
    try:
        listing = httpx.get(
            f"{settings.gemini_base_url.rstrip('/')}/v1beta/models",
            headers={"x-goog-api-key": settings.gemini_api_key},
            params={"pageSize": 200},
            timeout=15,
        )
        bidi = {
            m.get("name", "").removeprefix("models/")
            for m in listing.json().get("models", [])
            if "bidiGenerateContent" in (m.get("supportedGenerationMethods") or [])
        }
        if bidi and settings.gemini_live_model not in bidi:
            return Check(
                "Gemini (interview fallback)",
                "warn",
                f"Text model works, but {settings.gemini_live_model} is not a live-audio "
                "model — the voice call would fail",
                "Set GEMINI_LIVE_MODEL to one of: " + ", ".join(sorted(bidi)[:4]),
                meta={"available_live_models": sorted(bidi)},
            )
        live_note = " · live model listed"
    except (httpx.HTTPError, ValueError):
        pass

    return Check("Gemini (interview fallback)", "ok", f"key works · {detail}{live_note}")


def _google(probe: bool) -> Check:
    """Never runs the consent flow — that would open a browser on the server."""
    settings = get_settings()
    creds_file = Path(settings.google_credentials_file)
    token_file = Path(settings.google_token_file)

    if not creds_file.exists():
        return Check(
            "Google (forms + email)",
            "error",
            f"No OAuth client at {creds_file}",
            "Follow docs/GOOGLE_SETUP.md and save credentials.json there.",
        )
    if not token_file.exists():
        return Check(
            "Google (forms + email)",
            "warn",
            "OAuth client present, but not signed in yet",
            "Run `python -m app.cli google-login` (opens a browser once).",
        )

    try:
        from google.oauth2.credentials import Credentials

        from app.google_api.auth import SCOPES

        creds = Credentials.from_authorized_user_info(
            json.loads(token_file.read_text(encoding="utf-8")), SCOPES
        )
    except Exception as exc:
        return Check(
            "Google (forms + email)",
            "error",
            f"Cached token is unreadable: {str(exc)[:140]}",
            "Delete secrets/token.json and run `google-login` again.",
        )

    if creds.valid:
        return Check("Google (forms + email)", "ok", "Signed in, token valid")

    if not creds.refresh_token:
        return Check(
            "Google (forms + email)",
            "error",
            "Token expired and there is no refresh token",
            "Run `python -m app.cli google-login` again.",
        )
    if not probe:
        return Check(
            "Google (forms + email)",
            "ok",
            "Signed in, token expired but refreshable (not probed)",
        )

    try:
        from google.auth.transport.requests import Request

        creds.refresh(Request())
    except Exception as exc:
        return Check(
            "Google (forms + email)",
            "error",
            f"Token refresh failed: {str(exc)[:140]}",
            "Run `python -m app.cli google-login` again. Unverified apps expire refresh "
            "tokens after 7 days.",
        )
    return Check("Google (forms + email)", "ok", "Signed in, token refreshed")


def _delivery_mode() -> Check:
    settings = get_settings()
    if settings.notify_dry_run:
        return Check(
            "Message delivery",
            "warn",
            "DRY RUN — messages are printed, not sent",
            "Set NOTIFY_DRY_RUN=false in .env when you are ready to contact real candidates.",
        )
    return Check(
        "Message delivery",
        "ok",
        "LIVE — invitations will reach real candidates",
    )


def _admin_access() -> Check:
    """The check that matters most once this server leaves localhost."""
    settings = get_settings()
    url = settings.interview_base_url
    public = not any(h in url for h in ("127.0.0.1", "localhost", "0.0.0.0"))

    if settings.admin_password:
        return Check(
            "Admin access",
            "ok",
            f"Password protected, sign in as '{settings.admin_username}'",
        )
    if public:
        return Check(
            "Admin access",
            "error",
            f"NO PASSWORD, and the server is public at {url}",
            "Set ADMIN_PASSWORD in .env immediately — anyone with that URL can read every "
            "candidate's details and send invitations.",
        )
    return Check(
        "Admin access",
        "warn",
        "No password — fine for localhost, not for anything reachable from outside",
        "Set ADMIN_PASSWORD in .env before exposing this server.",
    )


def _interview_url() -> Check:
    settings = get_settings()
    url = settings.interview_base_url
    local = any(host in url for host in ("127.0.0.1", "localhost", "0.0.0.0"))
    if local:
        return Check(
            "Interview join URL",
            "warn",
            f"{url} — only reachable on this machine",
            "Candidates need a public https address. Browsers also block camera and "
            "microphone access on plain http from a remote origin. Use a tunnel "
            "(ngrok/Cloudflare) or deploy, then set INTERVIEW_BASE_URL.",
        )
    if url.startswith("http://"):
        return Check(
            "Interview join URL",
            "error",
            f"{url} is not https",
            "Browsers refuse camera and microphone access on remote http. Use https.",
        )
    return Check("Interview join URL", "ok", url)


def collect(probe: bool = False) -> dict:
    """Run every check and roll them into one payload for the dashboard."""
    checks = [
        _database(),
        _interview_database(),
        _screening_backend(probe),
        _openai(probe),
        _gemini(probe),
        _google(probe),
        _admin_access(),
        _delivery_mode(),
        _interview_url(),
    ]

    states = [c.state for c in checks]
    if "error" in states:
        overall = "error"
    elif "warn" in states:
        overall = "warn"
    else:
        overall = "ok"

    return {
        "overall": overall,
        "probed": probe,
        "checks": [asdict(c) for c in checks],
    }
