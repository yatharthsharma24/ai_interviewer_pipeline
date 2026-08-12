from __future__ import annotations

import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.config import get_settings

SCOPES = [
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/forms.responses.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.send",
]


class GoogleAuthError(RuntimeError):
    pass


def get_credentials(*, allow_interactive: bool = False) -> Credentials:
    """Load cached OAuth credentials, refreshing them when possible.

    ``allow_interactive`` gates the browser consent flow, and defaults to **off**.

    That default matters. ``InstalledAppFlow.run_local_server()`` blocks until somebody
    completes a Google sign-in in a browser *on this machine*. Called from a web request —
    say, clicking "Generate a form" in the dashboard — it would hang that request forever
    with no error and no way for the user to know why. Only ``app.cli google-login`` passes
    ``allow_interactive=True``; everything else gets a clear "sign in first" error.
    """
    settings = get_settings()
    token_path = Path(settings.google_token_file)
    creds: Credentials | None = None

    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_info(
                json.loads(token_path.read_text(encoding="utf-8")), SCOPES
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise GoogleAuthError(
                f"{token_path} is not a valid token file. Delete it and re-run `google-login`."
            ) from exc

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise GoogleAuthError(
                f"Google rejected the cached token: {exc}\n"
                "Delete secrets/token.json and run `python -m app.cli google-login` again. "
                "Unverified apps in Testing mode expire refresh tokens after 7 days."
            ) from exc
    else:
        cred_path = Path(settings.google_credentials_file)
        if not cred_path.exists():
            raise GoogleAuthError(
                f"Missing OAuth client file at {cred_path}.\n"
                "Create a Desktop-app OAuth client in Google Cloud Console, download the JSON, "
                "and save it there. See docs/GOOGLE_SETUP.md."
            )
        if not allow_interactive:
            raise GoogleAuthError(
                "Not signed in to Google yet.\n"
                "Run `python -m app.cli google-login` in a terminal on this machine — it "
                "opens a browser once and caches the token — then retry.\n"
                "(Sign-in cannot happen from the dashboard: it would need a browser on the "
                "server, not on yours.)"
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), SCOPES)
        creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_service(name: str, version: str):
    return build(name, version, credentials=get_credentials(), cache_discovery=False)
