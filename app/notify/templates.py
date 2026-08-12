"""Message bodies for interview invitations.

Invitations go out by email, which can carry structure and detail.

The time is always stated in the round's timezone with the zone named, because a candidate
in a different zone otherwise has to guess.
"""

from __future__ import annotations

from app.models import Candidate, InterviewMode, InterviewRound, InterviewSlot


def _first_name(candidate: Candidate) -> str:
    if not candidate.full_name:
        return "there"
    return candidate.full_name.strip().split()[0]


def _when(slot: InterviewSlot, round_: InterviewRound) -> str:
    start = slot.local_start()
    end = slot.local_end()
    return f"{start:%A, %d %B %Y} at {start:%H:%M}–{end:%H:%M} ({round_.timezone})"


def _where(round_: InterviewRound, join_url: str | None = None) -> tuple[str, str]:
    """Return (label, value) describing where the interview happens.

    A per-candidate ``join_url`` (Part 3's AI interview link) always wins over the round's
    shared location — the link is unique to this person and is how they are identified.
    """
    mode = InterviewMode(round_.mode)
    if join_url:
        return "Your personal interview link", join_url
    if mode is InterviewMode.online:
        return "Joining link", round_.location or "will be shared before the interview"
    if mode is InterviewMode.onsite:
        return "Address", round_.location or "will be shared before the interview"
    return "Format", "Telephone call — please keep your phone available"


def invitation_subject(job_title: str, round_: InterviewRound) -> str:
    return f"{round_.name} — {job_title}"


def invitation_email(
    candidate: Candidate,
    slot: InterviewSlot,
    round_: InterviewRound,
    join_url: str | None = None,
) -> str:
    job = round_.job
    where_label, where_value = _where(round_, join_url)

    lines = [
        f"Hi {_first_name(candidate)},",
        "",
        f"Good news — you have been shortlisted for {job.title}"
        + (f" at {job.department}" if job.department else "")
        + f", and we would like to invite you to {round_.name}.",
        "",
        "Your interview details:",
        f"  When: {_when(slot, round_)}",
        f"  Duration: {round_.slot_minutes} minutes",
        f"  {where_label}: {where_value}",
    ]

    if join_url:
        lines += [
            "",
            "This is an AI-conducted voice interview. Open the link at your scheduled time in "
            "Chrome or Edge on a laptop or desktop, and allow camera and microphone access.",
            "Please stay on the interview tab and look at the camera throughout — the session "
            "is monitored.",
        ]

    if round_.instructions:
        lines += ["", round_.instructions]

    lines += [
        "",
        "Please confirm you can make this time by replying to this email.",
    ]
    if round_.contact_email:
        lines.append(f"If the time does not work, write to {round_.contact_email} and we will rearrange.")

    lines += ["", "Best regards,", "Talent Team"]
    return "\n".join(lines)


def rejection_email(candidate: Candidate, round_: InterviewRound) -> str:
    """Only sent when the admin explicitly asks; the default is to notify no one."""
    return "\n".join(
        [
            f"Hi {_first_name(candidate)},",
            "",
            f"Thank you for applying for {round_.job.title} and for the time you put into "
            "your application.",
            "",
            "After reviewing everything we received, we will not be moving forward with your "
            "application on this occasion. This was a competitive process and the decision "
            "was not an easy one.",
            "",
            "We would genuinely welcome an application from you for future openings.",
            "",
            "Best wishes,",
            "Talent Team",
        ]
    )
