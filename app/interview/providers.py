"""Two model providers behind one interface, with automatic failover.

Part 3 calls a model in three places — planning the questions, running the live voice call,
and grading the transcript. Each one has an OpenAI implementation and a Gemini one, and each
tries the configured primary first and falls back to the other when the primary does not
answer.

Why fail over on *any* error rather than only on outages: a candidate is sitting on a call
link at a scheduled time. A 401 from a rotated key, a 429 from an exhausted quota, and a
connection reset all look the same from their side — the interview does not happen. Trying
the other provider costs one extra round trip and turns most of those into a completed
interview. Which provider actually served each call is recorded, so a silent failover still
shows up in the logs and in the interview record rather than being invisible.

Only the *text* providers live here. The live voice call is in ``realtime.py`` (OpenAI
WebRTC) and ``live_proxy.py`` (Gemini Live over a WebSocket), because their transports have
nothing in common.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """A single provider failed. Carries the provider name so failover can report both."""

    def __init__(self, provider: str, message: str):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.detail = message


class AllProvidersFailed(RuntimeError):
    """Every configured provider failed. Lists each reason — one of them is the real cause."""

    def __init__(self, failures: list[ProviderError]):
        self.failures = failures
        if not failures:
            body = (
                "No model provider is configured. Set OPENAI_API_KEY or GEMINI_API_KEY "
                "in .env."
            )
        else:
            body = "Every model provider failed:\n" + "\n".join(
                f"  - {failure}" for failure in failures
            )
        super().__init__(body)


_GEMINI_DROPPED_KEYS = frozenset(
    {"additionalProperties", "title", "default", "$schema", "$defs", "strict", "examples"}
)


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a Pydantic JSON schema into the OpenAPI subset Gemini accepts.

    Two transformations matter. Nested models become ``$ref`` pointers into ``$defs``, which
    Gemini will not follow, so every reference is inlined. And ``additionalProperties: false``
    — which OpenAI's strict mode *requires* — makes Gemini reject the request outright, so it
    and the other decorative keys are stripped.

    Inlining is safe here because the interview schemas are shallow trees. A self-referential
    schema would recurse forever, so it is refused rather than silently truncated.
    """
    defs = schema.get("$defs", {})

    def resolve(node: Any, seen: tuple[str, ...] = ()) -> Any:
        if isinstance(node, list):
            return [resolve(item, seen) for item in node]
        if not isinstance(node, dict):
            return node

        if "$ref" in node:
            ref = node["$ref"]
            name = ref.rsplit("/", 1)[-1]
            if name in seen:
                raise ValueError(
                    f"Schema {name!r} is recursive; Gemini's responseSchema cannot express "
                    "that. Flatten the model."
                )
            if name not in defs:
                raise ValueError(f"Unresolvable $ref {ref!r}.")
            return resolve(defs[name], (*seen, name))

        if "allOf" in node and len(node["allOf"]) == 1:
            merged = {k: v for k, v in node.items() if k != "allOf"}
            inlined = resolve(node["allOf"][0], seen)
            if isinstance(inlined, dict):
                return {**inlined, **{k: v for k, v in merged.items() if k not in _GEMINI_DROPPED_KEYS}}

        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _GEMINI_DROPPED_KEYS:
                continue
            out[key] = resolve(value, seen)
        return out

    converted = resolve({k: v for k, v in schema.items() if k != "$defs"})
    if not isinstance(converted, dict):  # pragma: no cover - a top-level schema is an object
        raise ValueError("Top-level schema must be an object.")
    return converted


class TextProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def describe(self) -> str: ...

    def complete_json(self, system: str, user: str, schema_name: str, schema: dict) -> str: ...


class OpenAITextProvider:
    name = "openai"

    def available(self) -> bool:
        return bool(get_settings().openai_api_key)

    def describe(self) -> str:
        return f"OpenAI {get_settings().grading_model}"

    def complete_json(self, system: str, user: str, schema_name: str, schema: dict) -> str:
        import openai

        settings = get_settings()
        if not settings.openai_api_key:
            raise ProviderError(self.name, "OPENAI_API_KEY is not set.")

        client = openai.OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
            timeout=settings.openai_timeout,
        )
        try:
            response = client.chat.completions.create(
                model=settings.grading_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                },
            )
        except Exception as exc:
            raise ProviderError(self.name, str(exc)) from exc

        choice = response.choices[0]
        if getattr(choice.message, "refusal", None):
            raise ProviderError(self.name, f"declined: {choice.message.refusal}")
        if not choice.message.content:
            raise ProviderError(
                self.name, f"returned nothing (finish={choice.finish_reason!r})"
            )
        return choice.message.content


class GeminiTextProvider:
    """Gemini via the REST API.

    Deliberately not the ``google-genai`` SDK: this needs one endpoint, the codebase already
    talks to OpenAI's realtime API the same way, and it keeps the dependency list short.
    """

    name = "gemini"

    def available(self) -> bool:
        return bool(get_settings().gemini_api_key)

    def describe(self) -> str:
        return f"Gemini {get_settings().gemini_model}"

    def complete_json(self, system: str, user: str, schema_name: str, schema: dict) -> str:
        settings = get_settings()
        if not settings.gemini_api_key:
            raise ProviderError(self.name, "GEMINI_API_KEY is not set.")

        url = (
            f"{settings.gemini_base_url.rstrip('/')}/v1beta/models/"
            f"{settings.gemini_model}:generateContent"
        )
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": to_gemini_schema(schema),
            },
        }

        try:
            response = httpx.post(
                url,
                headers={
                    "x-goog-api-key": settings.gemini_api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=settings.gemini_timeout,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"could not reach the API: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:300]
            try:
                detail = response.json().get("error", {}).get("message", detail)
            except ValueError:
                pass
            raise ProviderError(self.name, f"HTTP {response.status_code}: {detail}")

        try:
            body = response.json()
            candidate = body["candidates"][0]
        except (ValueError, KeyError, IndexError) as exc:
            block = (response.json() or {}).get("promptFeedback", {}).get("blockReason")
            if block:
                raise ProviderError(self.name, f"prompt blocked ({block})") from exc
            raise ProviderError(self.name, f"unexpected response shape: {response.text[:200]}") from exc

        finish = candidate.get("finishReason")
        parts = candidate.get("content", {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        if not text:
            raise ProviderError(self.name, f"returned nothing (finish={finish!r})")
        return text


PROVIDERS: dict[str, TextProvider] = {
    "openai": OpenAITextProvider(),
    "gemini": GeminiTextProvider(),
}


def provider_order() -> list[str]:
    """Primary first, then the other one. Unknown settings fall back to OpenAI-first."""
    primary = (get_settings().interview_provider or "openai").strip().lower()
    if primary not in PROVIDERS:
        logger.warning(
            "INTERVIEW_PROVIDER=%r is not a known provider; using 'openai'.", primary
        )
        primary = "openai"
    return [primary, *(name for name in PROVIDERS if name != primary)]


def configured_providers() -> list[str]:
    """Those that actually have a key, in preference order."""
    return [name for name in provider_order() if PROVIDERS[name].available()]


def complete_json(
    system: str, user: str, *, schema_name: str, schema: dict
) -> tuple[dict, str]:
    """Ask for a JSON object matching ``schema``. Returns (parsed, provider_name).

    Tries each configured provider in order and only raises once they have all failed, with
    every reason attached — the first failure is not necessarily the informative one.
    """
    failures: list[ProviderError] = []

    for name in provider_order():
        provider = PROVIDERS[name]
        if not provider.available():
            continue
        try:
            raw = provider.complete_json(system, user, schema_name, schema)
            parsed = json.loads(raw)
        except ProviderError as exc:
            logger.warning("%s failed (%s); trying the next provider.", name, exc.detail)
            failures.append(exc)
            continue
        except json.JSONDecodeError as exc:
            logger.warning("%s returned invalid JSON (%s); trying the next provider.", name, exc)
            failures.append(ProviderError(name, f"returned invalid JSON: {exc}"))
            continue

        if failures:
            logger.info("Recovered on %s after %d failure(s).", name, len(failures))
        return parsed, name

    raise AllProvidersFailed(failures)
