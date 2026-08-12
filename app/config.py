from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SECRETS_DIR = BASE_DIR / "secrets"
EXPORT_DIR = BASE_DIR / "exports"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = f"sqlite:///{(DATA_DIR / 'interview_pipeline.db').as_posix()}"

    screening_provider: str = "ollama"
    screening_model: str | None = None

    ollama_base_url: str = "http://localhost:11434"
    ollama_num_ctx: int = 8192
    ollama_keep_alive: str = "10m"
    ollama_timeout: float = 300.0

    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_timeout: float = 120.0
    screening_effort: str = "medium"

    gemini_api_key: str | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    gemini_model: str = "gemini-flash-latest"
    gemini_live_model: str = "gemini-2.5-flash-native-audio-latest"
    gemini_live_voice: str = "Kore"
    gemini_timeout: float = 120.0
    interview_provider: str = "openai"

    google_credentials_file: Path = SECRETS_DIR / "credentials.json"
    google_token_file: Path = SECRETS_DIR / "token.json"

    notify_dry_run: bool = True
    default_timezone: str = "Asia/Kolkata"

    interview_database_url: str = (
        f"sqlite:///{(DATA_DIR / 'interviews.db').as_posix()}"
    )
    realtime_model: str = "gpt-realtime-2.1-mini"
    realtime_voice: str = "alloy"
    realtime_transcribe_model: str = "gpt-4o-mini-transcribe"
    grading_model: str = "gpt-5-mini"
    interview_base_url: str = "http://127.0.0.1:8000"
    interview_max_minutes: int = 45

    proctor_look_away_seconds: float = 4.0
    proctor_no_face_seconds: float = 8.0
    proctor_tab_hidden_seconds: float = 3.0
    proctor_max_violations: int = 4

    admin_password: str | None = None
    admin_username: str = "admin"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    def ensure_dirs(self) -> None:
        for d in (DATA_DIR, SECRETS_DIR, EXPORT_DIR):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
