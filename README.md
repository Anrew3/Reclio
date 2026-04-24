<div align="center">

# Reclio

**Netflix-style personalized recommendations for [Chillio](https://chillio.app), powered by your Trakt.**

[![Python](https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![ChillLink Protocol](https://img.shields.io/badge/ChillLink-Protocol-7B5CFF)](https://chillio.app)
[![CI](https://img.shields.io/github/actions/workflow/status/Anrew3/reclio/ci.yml?branch=main&label=CI)](https://github.com/Anrew3/reclio/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-16A34A)](#license)

### [Try it now → **reclio.p0xl.com**](https://reclio.p0xl.com)

_Connect your Trakt. Copy your addon URL. Paste into Chillio. Done._

</div>

---

## What it is

Reclio is a **ChillLink Protocol addon server**. It reads your Trakt
watch history and returns 22 personalized feeds to
[Chillio](https://chillio.app) every time the app opens.

Use the public instance at [**reclio.p0xl.com**](https://reclio.p0xl.com),
or run your own with a single `docker compose up`.

## What you get

- **22 personalized rows** — *Because You Watched*, hidden gems, director spotlights, decade throwbacks, collaborative recs, trending, and more.
- **Trakt-native** — learns from watches, ratings, and watchlist.
- **Collaborative filtering** via Recombee for "Recommended For You" rows.
- **Pluggable LLM** — Ollama (default), Claude, OpenAI, or off.
- **Adaptive sync** — active users re-sync faster, idle users slower.
- **Never 5xx** — every external service can fail and `/feeds` still returns a valid response.
- **iOS-style portal** — dark palette, SF-Pro type, zero JS frameworks.
- **Self-hostable** — Docker Compose + Caddy with auto-HTTPS.

---

## Quick start — for users

1. Open **[reclio.p0xl.com](https://reclio.p0xl.com)** in any browser.
2. Click **Connect with Trakt** and authorize.
3. Tap **Copy** on the addon URL shown on your dashboard.
4. Open **Chillio** → **Settings → ChillLink Servers → Add Server** → paste → Save.
5. Go back to the Chillio home tab. Your 22 rows appear.

First-time users see mostly generic feeds for ~2 minutes while the
taste profile builds, then pull-to-refresh.

---

## Self-host

You need Docker, a domain name, and four free API keys: Trakt, TMDB,
Recombee, and optionally an LLM provider.

```bash
git clone https://github.com/Anrew3/reclio.git && cd reclio
cp .env.example .env

# Generate security keys
python3 -c "from cryptography.fernet import Fernet; print('FERNET_KEY=' + Fernet.generate_key().decode())" >> .env
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))" >> .env

# Edit .env — fill TRAKT_*, TMDB_API_KEY, RECOMBEE_*, set BASE_URL
# Edit Caddyfile — replace the hostname with your domain

docker compose up -d --build
docker compose logs -f app
```

First boot takes 3–5 min: the embedding model downloads (~100 MB),
Ollama pulls its model if enabled (~2 GB), and the initial TMDB sync
warms the catalog. Full deploy guide in
[**docs/setup**](docs/docs/setup.md).

---

## Configuration

### Required

| Variable | Description |
| --- | --- |
| `TRAKT_CLIENT_ID` / `TRAKT_CLIENT_SECRET` | Trakt OAuth app |
| `TMDB_API_KEY` | TMDB v3 API key |
| `RECOMBEE_DATABASE_ID` / `RECOMBEE_PRIVATE_TOKEN` | Recombee database |
| `FERNET_KEY` | 32-byte base64 key; encrypts Trakt tokens at rest |
| `SECRET_KEY` | Session cookie signing key |
| `BASE_URL` | Public URL, no trailing slash |

### LLM provider

Pick one — or `none` if you don't want any LLM calls.

| Variable | Default | Description |
| --- | --- | --- |
| `LLM_PROVIDER` | `ollama` | `ollama` · `claude` · `openai` · `none` |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | `http://ollama:11434` / `llama3.2:3b` | Local model |
| `ANTHROPIC_API_KEY` / `CLAUDE_MODEL` | — / `claude-haiku-4-5` | Anthropic |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | — / `gpt-4o-mini` | OpenAI |

Missing API key silently falls back to plain f-strings. Switching is a
single env var — no rebuild. See
[**docs/model-integrations**](docs/docs/model-integrations.md).

### Adaptive sync

Per-user sync cadence adapts to `/feeds` hit rate over the last 7 days.

| Variable | Default | Bucket |
| --- | --- | --- |
| `USER_SYNC_HOT_INTERVAL_HOURS` | `4` | ≥ 14 hits/week |
| `USER_SYNC_DEFAULT_INTERVAL_HOURS` | `8` | everyone else |
| `USER_SYNC_COLD_INTERVAL_HOURS` | `24` | ≤ 3 hits/week |
| `USER_SYNC_SWEEP_INTERVAL_HOURS` | `1` | scheduler tick |

Set all three intervals equal for a fixed schedule. See
[**docs/adaptive-sync**](docs/docs/adaptive-sync.md).

### Optional

| Variable | Default | Description |
| --- | --- | --- |
| `ADMIN_TOKEN` | _(blank = disabled)_ | Enables `/admin/*` endpoints |
| `CONTENT_SYNC_INTERVAL_HOURS` | `24` | Global TMDB catalog refresh |
| `TOKEN_REFRESH_INTERVAL_HOURS` | `6` | Trakt OAuth token refresh |
| `PORT` | `8000` | Internal app port |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/db/reclio.db` | SQLAlchemy URL |
| `CHROMA_PERSIST_DIR` | `./data/chroma` | ChromaDB path |

---

## How it works

1. You connect Trakt once on the Reclio portal.
2. Reclio fetches your history, ratings, and watchlist.
3. It builds a taste profile: genres, top actors/directors, decade, recent watches.
4. It pushes signals to Recombee and maintains two managed Trakt lists.
5. It gives you a personal addon URL to paste into Chillio.
6. Every `/feeds` hit returns 22 personalized rows.

Background jobs keep it fresh: content re-syncs daily, profiles adapt
per-user (4–24h), OAuth tokens refresh every 6h.

<details>
<summary><strong>Recombee deep-dive</strong></summary>

Recombee powers the two "Recommended For You" rows (feeds #2, #3).
Everything else is derived from Trakt + TMDB directly.

- Catalog: every TMDB title pushed with title/overview/genres/year/popularity/cast/director. `item_id` is `movie_{tmdb_id}` or `tv_{tmdb_id}` to avoid collisions.
- Interactions: watches → `AddDetailView`, ratings → `AddRating` (normalized to `[-1.0, 1.0]`), watchlist → `AddBookmark`.
- Materialized: top 50 movies and top 50 shows get pushed into managed Trakt lists. `/feeds` reads those lists directly — no in-band Recombee call, zero latency hit.
- Degrades: if Recombee is unavailable, feeds #2 and #3 fall back to a TMDB `discover` query filtered by your top genres.

Full details in [**docs/recombee**](docs/docs/recombee.md).

</details>

<details>
<summary><strong>Tech stack</strong></summary>

| Layer | Component |
| --- | --- |
| Language | Python 3.12+ |
| API | FastAPI + uvicorn |
| Database | SQLite (SQLAlchemy 2.0 async + aiosqlite) |
| Scheduler | APScheduler |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2` |
| Vector store | ChromaDB (embedded) |
| Recs engine | Recombee |
| LLM | Ollama · Claude · OpenAI · none |
| Frontend | Jinja2 + iOS-style CSS |
| Reverse proxy | Caddy (default) or Traefik |

</details>

---

## API

### ChillLink

```
GET /manifest
GET /feeds?user_id=<uuid>&username=<trakt>&session_id=<device>&last_watched=movie:<tmdb_id>
```

`user_id` is the primary key. `username` acts as a fallback when the
ID isn't known yet (fresh install on a new device). `session_id` is
logged for per-device analytics. `last_watched` lets Chillio signal a
live watch without waiting for the next Trakt sync. Full docs:
[**docs/api-reference**](docs/docs/api-reference.md).

**Feed IDs are stable but contextual.** BYW rows include the TMDB id
of the watched title (`because_watched_movie_tmdb_157336`), so when
the user's latest watch changes, Chillio sees a new feed rather than
silently changing the title of an existing one.

### Admin

Set `ADMIN_TOKEN` to enable `/admin/*`. Blank = off (returns 503). All
requests require header `X-Admin-Token: <token>`.

```bash
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" https://<host>/admin/sync/content
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" https://<host>/admin/sync/user/<user_id>
curl       -H "X-Admin-Token: $ADMIN_TOKEN" https://<host>/admin/status
```

---

## Caddy vs Traefik

The repo ships with **Caddy** (two-line `Caddyfile`, auto-HTTPS). If
you already run a Traefik stack, swap in Traefik labels instead — no
code changes. Full guide: [**docs/caddy-vs-traefik**](docs/docs/caddy-vs-traefik.md).

- Solo Reclio deployment? **Keep Caddy.**
- Existing multi-service Traefik cluster? **Use Traefik labels.**

---

## Troubleshooting

**Rows with `source: trakt_list` are empty.**
Wait for the first `user_sync` to complete. Rows with
`source: tmdb_query` always resolve via TMDB.

**`Trakt rejected the authorization`.** The Redirect URI on your
Trakt app must match `BASE_URL + /auth/callback` exactly, including
`https://`.

**OAuth state cookie rejected.** Cookies require `Secure`+`HttpOnly`
over HTTPS. `BASE_URL` must be HTTPS in prod and Caddy's cert must be
valid.

**Ollama keeps warning `model not found`.** First boot pulls the
model; on slow links this can take 10+ min. Reclio works fine without
an LLM — BYW titles fall back to plain f-strings.

**Recombee recommendations are empty.** Recombee needs a dozen+
items and interactions before it produces useful recs. The initial
`user_sync` seeds both.

---

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

uvicorn app.main:app --reload --port 8000
```

For local OAuth testing, tunnel with
[`cloudflared`](https://github.com/cloudflare/cloudflared) or
[`ngrok`](https://ngrok.com/) and set `BASE_URL` to the tunnel URL.

### Docs site

```bash
cd docs && npm install && npm start   # http://localhost:3000
```

---

## License

MIT.
