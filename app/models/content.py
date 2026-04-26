from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ContentCatalog(Base):
    __tablename__ = "content_catalog"

    tmdb_id: Mapped[str] = mapped_column(String, primary_key=True)  # e.g. "movie_550"
    media_type: Mapped[str] = mapped_column(String)  # "movie" or "tv"
    title: Mapped[str] = mapped_column(String)
    overview: Mapped[str | None] = mapped_column(String, nullable=True)
    genres: Mapped[list | None] = mapped_column(JSON, nullable=True)
    cast: Mapped[list | None] = mapped_column(JSON, nullable=True)
    director: Mapped[str | None] = mapped_column(String, nullable=True)
    keywords: Mapped[list | None] = mapped_column(JSON, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vote_average: Mapped[float | None] = mapped_column(Float, nullable=True)
    popularity: Mapped[float | None] = mapped_column(Float, nullable=True)
    embedding_stored: Mapped[bool] = mapped_column(Boolean, default=False)
    recombee_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    last_updated: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # --- v1.5 vector embedding columns ----------------------------------
    # Packed numpy.float32 array (arr.tobytes()). Dim varies by provider:
    # 384 (sentence-transformers MiniLM), 768 (Ollama nomic-embed-text),
    # 1536 (OpenAI text-embedding-3-small).
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String, nullable=True)
    # sha256(input_text)[:16] — skip re-embed when input unchanged.
    embedding_source_hash: Mapped[str | None] = mapped_column(String(16), nullable=True)
    embedding_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
