---
id: setup
title: Setup
sidebar_position: 2
---

# Setup

Reclio runs as a FastAPI app backed by SQLite. The recommended way to
run it is Docker Compose — the repo ships with a working `compose.yml`
and `Caddyfile`.

## Prerequisites

- Docker + Docker Compose
- A public domain with DNS pointing at the server (for HTTPS)
- API keys: [Trakt](https://trakt.tv/oauth/applications) and
  [TMDB](https://www.themoviedb.org/settings/api) — both free. That's
  the whole list: since v1.7 the recommendation engine runs locally.

## Docker Compose (recommended)

```bash
git clone https://github.com/Anrew3/reclio.git && cd reclio
cp .env.example .env

# Generate security keys
python3 -c "from cryptography.fernet import Fernet; print('FERNET_KEY=' + Fernet.generate_key().decode())" >> .env
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))" >> .env

# Edit .env — fill in TRAKT_*, TMDB_API_KEY, set BASE_URL
# Edit Caddyfile — replace the hostname with your domain

docker compose up -d --build
docker compose logs -f app    # watch the first content sync
```

First boot takes 3–5 minutes: `sentence-transformers` downloads the
embedding model (~100 MB), Ollama pulls its model if enabled (~2 GB),
and the initial TMDB sync warms the catalog.

## Getting API keys

### Trakt

1. Sign in at [trakt.tv/oauth/applications](https://trakt.tv/oauth/applications).
2. Click **New Application**.
3. Set **Redirect URI** to `https://<your-domain>/auth/callback`.
4. Copy **Client ID** → `TRAKT_CLIENT_ID`, **Client Secret** →
   `TRAKT_CLIENT_SECRET`.

### TMDB

1. Sign in at [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api).
2. Request an API key — the free Developer tier is plenty.
3. Copy the **API Read Access Token (v3)** → `TMDB_API_KEY`.

### Recombee (optional, legacy)

Not needed since v1.7 — the default `RECOMMENDER=local` engine runs
entirely on your box. If you specifically want the pre-1.7 behavior,
set `RECOMMENDER=recombee`, `pip install recombee-api-client`, and see
the [Recommendation engine](./recombee) page for the credentials.

## Local development (no Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys

uvicorn app.main:app --reload --port 8000
```

For local OAuth testing, tunnel with
[`cloudflared`](https://github.com/cloudflare/cloudflared) or
[`ngrok`](https://ngrok.com/) and set `BASE_URL` to the tunnel URL
(Trakt redirect URIs must be HTTPS).
