"""Send the application-form link out over Gmail."""

from __future__ import annotations

import base64
from email.message import EmailMessage

from googleapiclient.errors import HttpError

from app.google_api.auth import build_service
from app.models import JobOpening


class MailError(RuntimeError):
    pass


def default_invite_body(job: JobOpening) -> str:
    lines = [
        "Hello,",
        "",
        f"We are hiring for {job.title}"
        + (f" in the {job.department} team" if job.department else "")
        + (f", based in {job.location}" if job.location else "")
        + ".",
        "",
    ]
    if job.min_years_experience:
        lines.append(f"We are looking for {job.min_years_experience:g}+ years of experience.")
    if job.required_skills:
        lines.append("Must-have skills: " + ", ".join(job.required_skills) + ".")
    lines += [
        "",
        "If that sounds like you, apply here:",
        job.form_url or "(form link pending)",
        "",
        "Please fill in every mandatory question — incomplete applications are filtered out "
        "automatically before review.",
        "",
        "Best regards,",
        "Talent Team",
    ]
    return "\n".join(lines)


def send_form_link(
    job: JobOpening,
    recipients: list[str],
    *,
    subject: str | None = None,
    body: str | None = None,
    bcc: bool = True,
) -> dict[str, object]:
    
    if not job.form_url:
        raise MailError("This job has no form URL yet. Create or link a form first.")
    if not recipients:
        raise MailError("No recipients supplied.")

    service = build_service("gmail", "v1")

    message = EmailMessage()
    message["Subject"] = subject or f"We're hiring: {job.title}"
    if bcc:
        message["Bcc"] = ", ".join(recipients)
        message["To"] = "undisclosed-recipients:;"
    else:
        message["To"] = ", ".join(recipients)
    message.set_content(body or default_invite_body(job))

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    try:
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    except HttpError as exc:
        raise MailError(f"Gmail rejected the send: {exc}") from exc

    return {"message_id": sent.get("id"), "recipients": len(recipients)}
