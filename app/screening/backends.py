"""Pluggable scoring backends.

Both backends take the same three inputs (system prompt, user prompt, JSON schema) and
return raw JSON text constrained to that schema. Screening logic never knows which is in
use, so switching providers is a one-line `.env` change.

* **ollama** (default) — local, free, private. Candidate resumes never leave the machine.
  Uses Ollama's native ``/api/chat`` rather than its OpenAI-compat endpoint, because the
  compat layer cannot set ``num_ctx``: Ollama's default context window is small enough that
  a long job spec plus candidate profile gets silently truncated. Native also lets us pin
  ``temperature: 0`` so the same application scores the same way twice.
* **openai** — hosted OpenAI, Azure, or any OpenAI-compatible gateway.
"""

from __future__ import annotations

import json
from typing import Protocol

import httpx

from app.config import get_settings

_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")

_EFFORT_ALIASES = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}

DEFAULT_MODELS = {"ollama": "gemma4:latest", "openai": "gpt-5"}


class BackendError(RuntimeError):
    """Transport or configuration failure. Never a judgement about the candidate."""


class BackendRefusal(BackendError):
    """The model declined to answer. Surfaced to the admin, never silently scored."""


class ScoringBackend(Protocol):
    name: str

    def describe(self) -> str: ...

    def complete_json(self, system: str, user: str, schema: dict) -> str: ...


class OllamaBackend:
    """Local inference through Ollama's native chat API."""

    name = "ollama"

    def __init__(self, model: str, base_url: str, num_ctx: int, keep_alive: str, timeout: float):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self.timeout = timeout

    def describe(self) -> str:
        return f"ollama:{self.model} (num_ctx={self.num_ctx}) at {self.base_url}"

    def available_models(self) -> list[str]:
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=10)
            response.raise_for_status()
        except httpx.HTTPError:
            return []
        return [m.get("name", "") for m in response.json().get("models", [])]

    def complete_json(self, system: str, user: str, schema: dict) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "keep_alive": self.keep_alive,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": schema,
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
        }

        try:
            response = httpx.post(
                f"{self.base_url}/api/chat", json=payload, timeout=self.timeout
            )
        except httpx.ConnectError as exc:
            raise BackendError(
                f"Cannot reach Ollama at {self.base_url}. Start it (open the Ollama app, or "
                "run `ollama serve`) and try again."
            ) from exc
        except httpx.ReadTimeout as exc:
            raise BackendError(
                f"Ollama did not respond within {self.timeout:g}s. Local models are slow on "
                "first load — raise OLLAMA_TIMEOUT, or use a smaller model."
            ) from exc
        except httpx.HTTPError as exc:
            raise BackendError(f"Ollama request failed: {exc}") from exc

        if response.status_code == 404:
            installed = ", ".join(self.available_models()) or "none"
            raise BackendError(
                f"Ollama has no model named {self.model!r}. Pull it with "
                f"`ollama pull {self.model}`, or set SCREENING_MODEL to one you have "
                f"(installed: {installed})."
            )
        if response.status_code >= 400:
            raise BackendError(f"Ollama returned {response.status_code}: {response.text[:300]}")

        try:
            content = response.json()["message"]["content"]
        except (KeyError, ValueError, TypeError) as exc:
            raise BackendError(f"Unexpected Ollama response shape: {response.text[:300]}") from exc

        if not content or not content.strip():
            raise BackendError(
                "Ollama returned empty content. The prompt may have overflowed the context "
                f"window (num_ctx={self.num_ctx}) — raise OLLAMA_NUM_CTX."
            )
        return content


class OpenAIBackend:
    """Hosted OpenAI, Azure OpenAI, or any OpenAI-compatible gateway."""

    name = "openai"

    def __init__(self, model: str, api_key: str, base_url: str | None, effort: str, timeout: float):
        import openai

        self._openai = openai
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url or None, timeout=timeout)

    def describe(self) -> str:
        return f"openai:{self.model}"

    def _supports_reasoning_effort(self) -> bool:
        return self.model.lower().startswith(_REASONING_PREFIXES)

    def complete_json(self, system: str, user: str, schema: dict) -> str:
        openai = self._openai

        kwargs: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_completion_tokens": 8000,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "fit_assessment", "strict": True, "schema": schema},
            },
        }
        if self._supports_reasoning_effort():
            effort = _EFFORT_ALIASES.get(self.effort.lower())
            if effort:
                kwargs["reasoning_effort"] = effort

        try:
            response = self.client.chat.completions.create(**kwargs)
        except openai.BadRequestError as exc:
            raise BackendError(
                f"OpenAI rejected the request for model {self.model!r}: {exc}. Check that the "
                "model exists on your account and supports strict structured outputs."
            ) from exc
        except openai.AuthenticationError as exc:
            raise BackendError(f"OPENAI_API_KEY was rejected: {exc}") from exc
        except openai.RateLimitError as exc:
            raise BackendError(f"Rate limited or out of quota: {exc}") from exc
        except openai.APIStatusError as exc:
            raise BackendError(f"OpenAI API error {exc.status_code}: {exc}") from exc
        except openai.APIConnectionError as exc:
            raise BackendError(f"Could not reach the OpenAI API: {exc}") from exc

        if not response.choices:
            raise BackendError("OpenAI returned no choices.")
        choice = response.choices[0]

        refusal = getattr(choice.message, "refusal", None)
        if refusal:
            raise BackendRefusal(f"The model declined to score this application: {refusal}")

        if choice.finish_reason == "length":
            raise BackendError(
                "Response hit the token limit before completing. Lower SCREENING_EFFORT or "
                "raise max_completion_tokens."
            )

        content = choice.message.content
        if not content:
            raise BackendError(
                f"OpenAI returned empty content (finish_reason={choice.finish_reason!r})."
            )
        return content


_backend: ScoringBackend | None = None


def get_backend(force_reload: bool = False) -> ScoringBackend:
    global _backend
    if _backend is not None and not force_reload:
        return _backend

    settings = get_settings()
    provider = settings.screening_provider.strip().lower()
    model = settings.screening_model or DEFAULT_MODELS.get(provider)

    if provider == "ollama":
        _backend = OllamaBackend(
            model=model,
            base_url=settings.ollama_base_url,
            num_ctx=settings.ollama_num_ctx,
            keep_alive=settings.ollama_keep_alive,
            timeout=settings.ollama_timeout,
        )
    elif provider == "openai":
        if not settings.openai_api_key:
            raise BackendError(
                "SCREENING_PROVIDER=openai but OPENAI_API_KEY is not set. Add the key, "
                "switch to SCREENING_PROVIDER=ollama, or screen with --no-llm."
            )
        _backend = OpenAIBackend(
            model=model,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            effort=settings.screening_effort,
            timeout=settings.openai_timeout,
        )
    else:
        raise BackendError(
            f"Unknown SCREENING_PROVIDER {provider!r}. Valid values: ollama, openai."
        )

    return _backend
