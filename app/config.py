from functools import lru_cache
from pydantic import Field
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

    # Ollama
    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "llama3.2:3b"

    # Sync intervals
    user_sync_interval_hours: int = 4
    content_sync_interval_hours: int = 24
    token_refresh_interval_hours: int = 6

    # Storage
    database_url: str = "sqlite+aiosqlite:///./data/db/reclio.db"
    chroma_persist_dir: str = "./data/chroma"

    @property
    def trakt_redirect_uri(self) -> str:
        return f"{self.base_url.rstrip('/')}/auth/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()
