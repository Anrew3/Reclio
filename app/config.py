from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Trakt
    trakt_client_id: str = ""
    trakt_client_secret: str = ""

    # TMDB
    tmdb_api_key: str = ""

    # Recombee
    recombee_database_id: str = ""
    recombee_private_token: str = ""
    recombee_region: str = "us-west"  # us-west | eu-west | ap-se

    # Security
    fernet_key: str = ""
    secret_key: str = "change-me-in-production"
    admin_token: str = ""  # gate for /admin/* endpoints; empty disables admin

    # App
    base_url: str = "http://localhost:8000"
    port: int = 8000

    # -------- LLM --------
    # "none" disables LLM features entirely (faster, no network). The default
    # on the public instance is "claude"; self-hosters can point at Ollama.
    llm_provider: Literal["ollama", "claude", "openai", "none"] = "ollama"

    # Ollama
    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "llama3.2:3b"

    # Anthropic / Claude — used when llm_provider="claude"
    anthropic_api_key: str = ""
    claude_model: str = "claude-haiku-4-5"  # cheap + fast; bump to sonnet for quality

    # OpenAI — used when llm_provider="openai"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # -------- Sync --------
    # Default cadence for the user-sync sweep. Adaptive logic (see
    # app/jobs/user_sync.py) may sync a given user more or less often
    # based on how frequently Chillio is actually hitting /feeds.
    user_sync_default_interval_hours: int = 8
    user_sync_hot_interval_hours: int = 4  # heavy users
    user_sync_cold_interval_hours: int = 24  # dormant users
    # Request rates (hits / 7 days) that promote a user between bands.
    user_sync_hot_threshold_per_week: int = 14  # ~2/day
    user_sync_cold_threshold_per_week: int = 3  # fewer than ~3/week
    # How often the sweep *loop* wakes up to re-evaluate every user. Picking
    # a user actually to sync is decided inside the sweep by the adaptive
    # logic, so this is just the polling granularity.
    user_sync_sweep_interval_hours: int = 1

    content_sync_interval_hours: int = 24  # reserved — scheduler uses a daily cron
    token_refresh_interval_hours: int = 6

    # Storage
    database_url: str = "sqlite+aiosqlite:///./data/db/reclio.db"
    chroma_persist_dir: str = "./data/chroma"

    @property
    def trakt_redirect_uri(self) -> str:
        return f"{self.base_url.rstrip('/')}/auth/callback"

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider != "none"


@lru_cache
def get_settings() -> Settings:
    return Settings()
