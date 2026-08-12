"""Google Forms automation: build the application form, then pull responses back.

The Forms API has one sharp edge worth knowing: ``forms.create`` accepts *only*
``info.title`` and ``info.documentTitle``. Description and every question have to go
through a follow-up ``batchUpdate``. That is why form creation below is three calls.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from googleapiclient.errors import HttpError

from app.field_map import FIELDS, FIELDS_BY_KEY, FieldSpec, match_question_title
from app.google_api.auth import build_service
from app.models import JobOpening

FORM_URL_RE = re.compile(r"/forms/d/(?:e/)?([A-Za-z0-9_-]{20,})")


class FormError(RuntimeError):
    pass


def extract_form_id(form_ref: str) -> str:
    """Accept a bare form ID or any Google Forms URL (edit, view, or /d/e/ short link)."""
    ref = form_ref.strip()
    match = FORM_URL_RE.search(ref)
    if match:
        return match.group(1)
    if "/" in ref or " " in ref:
        raise FormError(f"Could not find a form ID in {form_ref!r}.")
    return ref


def _skill_options(job: JobOpening) -> list[str]:
    options: list[str] = []
    seen: set[str] = set()
    for skill in [*job.required_skills, *job.preferred_skills]:
        key = skill.strip().lower()
        if key and key not in seen:
            seen.add(key)
            options.append(skill.strip())
    return options or ["Not applicable"]


def _question_body(spec: FieldSpec, job: JobOpening, required: bool) -> dict[str, Any]:
    question: dict[str, Any] = {"required": required}

    if spec.qtype == "checkbox":
        question["choiceQuestion"] = {
            "type": "CHECKBOX",
            "options": [{"value": opt} for opt in _skill_options(job)],
            "shuffle": False,
        }
    elif spec.qtype == "paragraph":
        question["textQuestion"] = {"paragraph": True}
    else:
        question["textQuestion"] = {"paragraph": False}

    item: dict[str, Any] = {"title": spec.title, "questionItem": {"question": question}}
    if spec.description:
        item["description"] = spec.description
    return item


def _form_description(job: JobOpening) -> str:
    parts = [f"Application form for {job.title}."]
    if job.department:
        parts.append(f"Team: {job.department}.")
    if job.location:
        parts.append(f"Location: {job.location}.")
    if job.min_years_experience:
        parts.append(f"We are looking for {job.min_years_experience:g}+ years of experience.")
    if job.required_skills:
        parts.append("Must-have skills: " + ", ".join(job.required_skills) + ".")
    parts.append("All starred questions are mandatory — blank answers are filtered out automatically.")
    return " ".join(parts)


def create_form_for_job(job: JobOpening, field_keys: list[str] | None = None) -> dict[str, Any]:
    service = build_service("forms", "v1")
    keys = field_keys or [f.key for f in FIELDS]
    unknown = [k for k in keys if k not in FIELDS_BY_KEY]
    if unknown:
        raise FormError(f"Unknown field keys: {', '.join(unknown)}")

    required_set = set(job.required_fields or [])

    try:
        created = service.forms().create(
            body={"info": {"title": f"{job.title} — Application", "documentTitle": f"{job.title} Application"}}
        ).execute()
        form_id = created["formId"]

        service.forms().batchUpdate(
            formId=form_id,
            body={
                "requests": [
                    {
                        "updateFormInfo": {
                            "info": {"description": _form_description(job)},
                            "updateMask": "description",
                        }
                    }
                ]
            },
        ).execute()

        item_requests = [
            {
                "createItem": {
                    "item": _question_body(FIELDS_BY_KEY[key], job, required=key in required_set),
                    "location": {"index": index},
                }
            }
            for index, key in enumerate(keys)
        ]
        result = service.forms().batchUpdate(
            formId=form_id, body={"requests": item_requests}
        ).execute()
    except HttpError as exc:
        raise FormError(f"Google Forms API rejected the request: {exc}") from exc

    question_map: dict[str, str] = {}
    for key, reply in zip(keys, result.get("replies", []), strict=False):
        question_ids = reply.get("createItem", {}).get("questionId", [])
        if question_ids:
            question_map[key] = question_ids[0]

    return {
        "form_id": form_id,
        "form_url": created.get("responderUri"),
        "form_edit_url": f"https://docs.google.com/forms/d/{form_id}/edit",
        "question_map": question_map,
    }


def inspect_form(form_id: str) -> dict[str, Any]:
    service = build_service("forms", "v1")
    try:
        form = service.forms().get(formId=form_id).execute()
    except HttpError as exc:
        raise FormError(
            f"Could not read form {form_id}. Confirm the signed-in Google account has edit "
            f"access to it. ({exc})"
        ) from exc

    question_map: dict[str, str] = {}
    unmapped: list[dict[str, str]] = []

    for item in form.get("items", []):
        question = item.get("questionItem", {}).get("question")
        if not question:
            continue
        question_id = question.get("questionId")
        title = item.get("title", "")
        key = match_question_title(title)
        if key and key not in question_map and question_id:
            question_map[key] = question_id
        else:
            unmapped.append({"question_id": question_id or "", "title": title})

    return {
        "form_id": form_id,
        "title": form.get("info", {}).get("title", ""),
        "form_url": form.get("responderUri"),
        "form_edit_url": f"https://docs.google.com/forms/d/{form_id}/edit",
        "question_map": question_map,
        "unmapped_questions": unmapped,
    }


def list_responses(form_id: str) -> list[dict[str, Any]]:
    """Every response on the form, following pagination."""
    service = build_service("forms", "v1")
    responses: list[dict[str, Any]] = []
    page_token: str | None = None

    try:
        while True:
            kwargs: dict[str, Any] = {"formId": form_id}
            if page_token:
                kwargs["pageToken"] = page_token
            page = service.forms().responses().list(**kwargs).execute()
            responses.extend(page.get("responses", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                break
    except HttpError as exc:
        raise FormError(f"Could not list responses for form {form_id}: {exc}") from exc

    return responses


def answer_values(answer: dict[str, Any]) -> list[str]:
    if "textAnswers" in answer:
        return [a.get("value", "") for a in answer["textAnswers"].get("answers", [])]
    if "fileUploadAnswers" in answer:
        return [
            f"https://drive.google.com/file/d/{a['fileId']}/view"
            for a in answer["fileUploadAnswers"].get("answers", [])
            if a.get("fileId")
        ]
    return []


def parse_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
