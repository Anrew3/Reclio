from typing import Literal

from pydantic import BaseModel, Field


class SupportedEndpoints(BaseModel):
    feeds: str | None = "/feeds"
    streams: str | None = None


class Manifest(BaseModel):
    id: str = "reclio-recommendations"
    version: str = "1.0.0"
    name: str = "Reclio"
    description: str = (
        "Netflix-style personalized recommendations powered by your Trakt history"
    )
    supported_endpoints: SupportedEndpoints = Field(default_factory=SupportedEndpoints)


class FeedSourceMetadata(BaseModel):
    id: int | None = None
    path: str | None = None
    parameters: str | None = None


class Feed(BaseModel):
    id: str
    title: str
    source: Literal["trakt_list", "tmdb_query"]
    source_metadata: dict
    content_type: Literal["all", "movies", "shows"]
    blurb: str | None = None


class FeedsResponse(BaseModel):
    feeds: list[Feed]
