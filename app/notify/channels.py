"""Notification channels: email (Gmail), plus a dry-run.

Same shape as ``app/screening/backends.py`` — one interface, swappable implementations, so
the scheduling logic never knows how a message physically goes out.

Every channel returns a ``DeliveryResult`` rather than raising on a send failure. One
candidate's bad email address must not abort a round of fifty invitations, so failures are
recorded per recipient and reported at the end.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from app.config import get_settings


@dataclass
class DeliveryResult:
    channel: str
    ok: bool
    message_id: str | None = None
    error: str | None = None
    skipped: bool = False


class Channel(Protocol):
    name: str

    def describe(self) -> str: ...

    def send(self, to: str, subject: str, body: str) -> DeliveryResult: ...


class EmailChannel:
    """Gmail API, reusing the OAuth credentials Part 1 already set up."""

    name = "email"

    def describe(self) -> str:
        return "email via Gmail API"

    def send(self, to: str, subject: str, body: str) -> DeliveryResult:
        if not to or "@" not in to:
            return DeliveryResult(self.name, ok=False, skipped=True, error="No email address.")

        from googleapiclient.errors import HttpError

        from app.google_api.auth import GoogleAuthError, build_service

        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        try:
            service = build_service("gmail", "v1")
            sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        except GoogleAuthError as exc:
            return DeliveryResult(self.name, ok=False, error=f"Google auth: {exc}")
        except HttpError as exc:
            return DeliveryResult(self.name, ok=False, error=f"Gmail rejected the send: {exc}")

        return DeliveryResult(self.name, ok=True, message_id=sent.get("id"))


class DryRunChannel:
    """Prints instead of sending. Wraps a real channel's name so statuses look identical."""

    def __init__(self, name: str, sink=None):
        self.name = name
        self._sink = sink if sink is not None else []

    def describe(self) -> str:
        return f"{self.name} (DRY RUN — nothing is sent)"

    @property
    def sent(self) -> list[dict]:
        return self._sink

    def send(self, to: str, subject: str, body: str) -> DeliveryResult:
        self._sink.append({"channel": self.name, "to": to, "subject": subject, "body": body})
        return DeliveryResult(self.name, ok=True, message_id="dry-run", skipped=True)


class ChannelError(RuntimeError):
    pass


def build_channels(*, use_email: bool = True, dry_run: bool | None = None) -> list[Channel]:
    """Assemble the channels for a notification run.

    A channel that is requested but not configured is a hard error, not a silent skip —
    quietly sending only half the invitations is worse than refusing to start.
    """
    settings = get_settings()
    dry = settings.notify_dry_run if dry_run is None else dry_run

    channels: list[Channel] = []

    if use_email:
        channels.append(DryRunChannel("email") if dry else EmailChannel())

    if not channels:
        raise ChannelError("No channels enabled — nothing would be sent.")
    return channels
