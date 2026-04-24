from pydantic import BaseModel


class TraktIds(BaseModel):
    trakt: int | None = None
    slug: str | None = None
    imdb: str | None = None
    tmdb: int | None = None
    tvdb: int | None = None


class TraktMovie(BaseModel):
    title: str | None = None
    year: int | None = None
    ids: TraktIds | None = None


class TraktShow(BaseModel):
    title: str | None = None
    year: int | None = None
    ids: TraktIds | None = None


class TraktTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    created_at: int
    scope: str | None = None
    token_type: str | None = None
