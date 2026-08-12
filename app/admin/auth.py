"""Access control for the admin surface.

The moment this server is reachable from the internet — a tunnel, a deploy, anything — the
dashboard is reachable too. Without a gate, whoever has the URL can read every candidate's
name, email, phone, resume link and interview transcript, and can send real invitations.

So: everything is protected **except** the candidate call flow, which cannot be, because
candidates have no account. Those routes are authorised by the unguessable token in their
personal link instead.

Off by default, because forcing a password on `127.0.0.1` would be friction with no benefit.
Setting ``ADMIN_PASSWORD`` turns it on.
"""

from __future__ import annotations

import base64
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from app.config import get_settings


PUBLIC_PREFIXES: tuple[str, ...] = ("/interview/",)

PUBLIC_PATHS: frozenset[str] = frozenset({"/health"})


def is_public(path: str) -> bool:
    """True when a path is part of the candidate flow and must not require a password."""
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _unauthorized(detail: str) -> Response:
    return JSONResponse(
        status_code=401,
        content={"detail": detail},
        headers={"WWW-Authenticate": 'Basic realm="Interview Pipeline admin"'},
    )


def check_basic_auth(header: str | None, username: str, password: str) -> bool:
    """Validate an Authorization header against the configured credentials."""
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        given_user, _, given_pass = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False

    return secrets.compare_digest(given_user, username) and secrets.compare_digest(
        given_pass, password
    )


async def admin_auth_middleware(request: Request, call_next):
    """Gate everything except the candidate call flow, when a password is configured."""
    settings = get_settings()

    if not settings.admin_password:
        return await call_next(request)

    if is_public(request.url.path):
        return await call_next(request)

    if not check_basic_auth(
        request.headers.get("Authorization"),
        settings.admin_username,
        settings.admin_password,
    ):
        return _unauthorized("Admin credentials required.")

    return await call_next(request)
