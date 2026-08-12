"""Admin dashboard: static SPA plus the endpoints only it needs.

The dashboard is plain HTML/CSS/JS with no build step, driving the same JSON API the CLI
drives. That keeps one source of truth for behaviour — anything the dashboard can do, the
API can do, and there is no second implementation to drift.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.admin import status as status_checks

router = APIRouter()
STATIC_DIR = Path(__file__).resolve().parent / "static"


@router.get("/system/status")
def system_status(
    probe: bool = Query(
        default=False,
        description="Also make live calls (Ollama, OpenAI, Google token refresh). Slower.",
    ),
) -> dict:
    """Component-by-component health. This is what the dashboard's System page shows."""
    return status_checks.collect(probe=probe)


@router.get("/admin", response_class=HTMLResponse)
def admin_index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@router.get("/admin/app.js")
def admin_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.js", media_type="application/javascript")


@router.get("/admin/styles.css")
def admin_css() -> FileResponse:
    return FileResponse(STATIC_DIR / "styles.css", media_type="text/css")


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Land on the dashboard rather than a bare 404."""
    return RedirectResponse(url="/admin")
