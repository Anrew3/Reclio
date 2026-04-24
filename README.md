<div align="center">

# Reclio

**Netflix-style personalized recommendations for [Chillio](https://chillio.app), powered by your Trakt.**

[![Python](https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![ChillLink Protocol](https://img.shields.io/badge/ChillLink-Protocol-7B5CFF)](https://chillio.app)
[![License](https://img.shields.io/badge/license-MIT-16A34A)](#license)

### [Try it now → **reclio.p0xl.com**](https://reclio.p0xl.com)

_Connect your Trakt, grab your addon URL, paste into Chillio. No setup required._

</div>

---

## What is Reclio?

Reclio is a **ChillLink Protocol addon server** that turns your Trakt
watch history into 22 dynamically personalized feeds for the
[Chillio](https://chillio.app) app on iOS, macOS, and Apple TV.

It learns your taste — genre affinities, favorite actors and directors,
preferred decade, recent watches — and ships you a per-user addon URL.
Every time Chillio opens, Reclio hands back a Netflix-style grid tuned
to you: *Because You Watched*, hidden gems in your top genres,
collaborative-filtering recs, watch-progress, watchlist, trending, and
more.

You can use the [public instance at **reclio.p0xl.com**](https://reclio.p0xl.com),
or [self-host your own](#self-hosting) in a Docker Compose stack.

> _Screenshot: drop a shot of the iOS-style dashboard here._

---

## Features

- **22 personalized rows** — Because You Watched, recommended movies &
  shows, hidden gems, director spotlights, decade throwbacks, trending,
  and more. Order matches Chillio's Netflix-style layout.
- **Trakt-native** — we learn from your watch history, ratings, and
  watchlist. Ratings are normalized to Recombee's `[-1.0, 1.0]` scale,
  timestamps are preserved.
- **Managed Trakt lists** — Reclio creates three lists on your Trakt
  account (`Reclio • Recommended Movies`, `Reclio • Recommended Shows`,
  `Reclio • Watch Progress`) and keeps them fresh. You can browse them
  directly at trakt.tv.
- **Collaborative filtering** via Recombee — the "Recommended For You"
  rows are powered by a real recommendations engine, not hand-rolled
  heuristics.
- **LLM-generated row titles** — "Because You Watched Arrival" beats
  a generic heading. Uses a local Ollama model; falls back gracefully.
- **Graceful degradation** — every external service (Trakt, TMDB,
  Recombee, Ollama) can fail independently without breaking `/feeds`.
- **iOS-style portal** — dark palette, SF-Pro type, proper safe-area
  handling, zero JavaScript frameworks.
- **Self-hostable** — one `docker compose up` and you have your own
  instance with Caddy-managed HTTPS.

---

## Quick start — for users

**You do not need to install or run anything.** Reclio is an addon URL
you paste into Chillio.

1. Open **[reclio.p0xl.com](https://reclio.p0xl.com)** in any browser.
2. Click **Connect with Trakt** and authorize.
3. You land on a dashboard showing your **addon URL**. Tap **Copy**.
4. Open **Chillio** on your iPhone / Mac / Apple TV → **Settings →
   ChillLink Servers → Add Server** → paste → Save.
5. Go back to the Chillio home tab. Your 22 personalized rows appear.

First-time users may see mostly generic feeds for ~2 minutes while the
taste profile builds. Pull to refresh, then you're live.

> **Using someone else's Reclio deployment?** Same five steps — just
> start at their portal URL instead of `reclio.p0xl.com`.

---

## Self-hosting

Self-hosting is a first-class option. Your Trakt tokens never leave
your server, and you aren't dependent on anyone else's uptime.

### Prerequisites

- Docker + Docker Compose
- A public domain with DNS pointing at the server (Caddy handles TLS)
- Free API keys: Trakt, TMDB, Recombee

### Deploy

```bash
git clone https://github.com/Anrew3/reclio.git && cd reclio
cp .env.example .env

# Generate security keys
python3 -c "from cryptography.fernet import Fernet; print('FERNET_KEY=' + Fernet.generate_key().decode())" >> .env
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))" >> .env

# Edit .env → fill in TRAKT_*, TMDB_API_KEY, RECOMBEE_* and set BASE_URL
# Edit Caddyfile → replace reclio.p0xl.com with your domain

docker compose up -d --build
docker compose logs -f app    # watch the first content sync run
```

First boot takes ~5 minutes: `sentence-transformers` downloads the
embedding model (~100 MB), Ollama pulls `llama3.2:3b` (~2 GB), and the
initial TMDB sync warms the catalog.

Once `app` is ready, share your portal URL with users. Each one
connects with their own Trakt account — one Reclio instance serves
many users.

<details>
<summary><strong>Getting your Trakt API keys</strong></summary>

1. Sign in at <https://trakt.tv/oauth/applications>.
2. Click **New Application**.
3. Name it anything (e.g. `Reclio`).
4. **Redirect URI** must be `https://<your-domain>/auth/callback`.
5. Leave **Permissions** at defaults (read + list management).
6. Copy **Client ID** and **Client Secret** into `.env` as
   `TRAKT_CLIENT_ID` and `TRAKT_CLIENT_SECRET`.

</details>

<details>
<summary><strong>Getting your TMDB API key</strong></summary>

1. Sign in at <https://www.themoviedb.org/settings/api>.
2. Request an API key — the free "Developer" tier is plenty.
3. Copy the **API Read Access Token (v3)** into `.env` as
   `TMDB_API_KEY`.

</details>

<details>
<summary><strong>Getting your Recombee database</strong></summary>

1. Sign up at <https://www.recombee.com/>.
2. Create a new database — the free tier handles tens of thousands of
   items and millions of interactions.
3. From the database settings copy:
   - **Database ID** → `RECOMBEE_DATABASE_ID`
   - **Private token** → `RECOMBEE_PRIVATE_TOKEN`
4. Set `RECOMBEE_REGION` to match your database region
   (`us-west`, `eu-west`, or `ap-se`).

</details>

---

## Configuration

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `TRAKT_CLIENT_ID` | yes | — | Trakt OAuth app client id |
| `TRAKT_CLIENT_SECRET` | yes | — | Trakt OAuth app client secret |
| `TMDB_API_KEY` | yes | — | TMDB v3 API key |
| `RECOMBEE_DATABASE_ID` | yes | — | Recombee database id |
| `RECOMBEE_PRIVATE_TOKEN` | yes | — | Recombee private token |
| `RECOMBEE_REGION` | no | `us-west` | `us-west` · `eu-west` · `ap-se` |
| `FERNET_KEY` | yes | — | 32-byte base64 key for token encryption |
| `SECRET_KEY` | yes | `change-me-in-production` | Session signing key |
| `ADMIN_TOKEN` | no | _(blank = disabled)_ | Token for `/admin/*` endpoints |
| `BASE_URL` | yes | `http://localhost:8000` | Public URL (no trailing slash) |
| `PORT` | no | `8000` | Internal app port |
| `OLLAMA_BASE_URL` | no | `http://ollama:11434` | Ollama HTTP endpoint |
| `OLLAMA_MODEL` | no | `llama3.2:3b` | Ollama model name |
| `USER_SYNC_INTERVAL_HOURS` | no | `4` | Per-user sync cadence |
| `CONTENT_SYNC_INTERVAL_HOURS` | no | `24` | Global TMDB sync cadence |
| `TOKEN_REFRESH_INTERVAL_HOURS` | no | `6` | Trakt token refresh cadence |
| `DATABASE_URL` | no | `sqlite+aiosqlite:///./data/db/reclio.db` | SQLAlchemy URL |
| `CHROMA_PERSIST_DIR` | no | `./data/chroma` | ChromaDB storage path |

---

## How it works

<details>
<summary><strong>Plain-English walkthrough</strong></summary>

1. You connect Trakt once on the Reclio portal.
2. Reclio fetches your watch history, ratings, and watchlist.
3. It builds a *taste profile*: genre affinities, top actors and
   directors, preferred decade, last-watched titles.
4. It pushes those signals to Recombee and keeps two managed Trakt
   lists up to date with your top 50 movies and 50 shows.
5. It hands you a personal addon URL to paste into Chillio.
6. Every time Chillio opens, it calls `/feeds?user_id=<you>` and gets
   back 22 personalized rows in Netflix-style order.

Background jobs keep everything fresh: content re-syncs daily,
profiles every 4 hours, OAuth tokens every 6 hours.

</details>

<details>
<summary><strong>Recombee integration deep-dive</strong></summary>

[Recombee](https://www.recombee.com/) powers the two "Recommended For
You" rows (feeds #2 and #3). Everything else is derived directly from
your Trakt data or from TMDB. Recombee handles the collaborative
filtering that can't be computed from your history alone.

**Data flow:**

```
  ┌─────────────┐   daily     ┌──────────┐  items   ┌──────────┐
  │    TMDB     │────────────▶│  Reclio  │─────────▶│ Recombee │
  └─────────────┘             │ content  │          │ catalog  │
                              │  sync    │          └─────┬────┘
                              └──────────┘                │
                                                          │ train
  ┌─────────────┐  every 4h   ┌──────────┐ interactions   │
  │   Trakt     │────────────▶│  Reclio  │◀───────────────┘
  │  history,   │             │  user    │
  │  ratings,   │             │  sync    │   ranked IDs
  │  watchlist  │             └──────────┘─────────┐
  └─────────────┘                    │             ▼
                                     │       ┌──────────┐
                                     │       │ Recombee │
                                     │       │ recommend│
                                     │       └─────┬────┘
                                     │             │ 50 movie + 50 show IDs
                                     ▼             │
                              ┌──────────┐         │
                              │  Trakt   │◀────────┘
                              │ managed  │
                              │  lists   │
                              └──────────┘
                                     │
                                     ▼
                              ┌──────────┐
                              │ Chillio  │
                              │  /feeds  │
                              └──────────┘
```

**Catalog properties** we push per title
([`content_sync`](app/jobs/content_sync.py)):

| Property | Type | Source |
| --- | --- | --- |
| `title` | string | TMDB `title` or `name` |
| `overview` | string | TMDB `overview` |
| `genres` | set of strings | TMDB expanded genres |
| `year` | int | Parsed from release / first-air date |
| `vote_average` | double | TMDB `vote_average` |
| `popularity` | double | TMDB `popularity` |
| `media_type` | string | `"movie"` or `"tv"` |
| `cast` | set of strings | Top 5 from TMDB `credits.cast` |
| `director` | string | TMDB `credits.crew` where `job == "Director"` |

`item_id` is always `movie_{tmdb_id}` or `tv_{tmdb_id}` — the type
prefix prevents collisions between movies and shows that share a TMDB
numeric id.

**User interactions** ([`user_sync`](app/jobs/user_sync.py)):

| Trakt signal | Recombee call | Notes |
| --- | --- | --- |
| Watch history item | `AddDetailView(user, item, ts)` | Implicit positive signal |
| Rating (1–10) | `AddRating(user, item, score, ts)` | Normalized to `[-1.0, 1.0]`; Trakt 5.5 ≈ neutral, 10 → +1.0, 1 → -1.0 |
| Watchlist entry | `AddBookmark(user, item)` | Strong positive signal of intent |

`users.last_history_sync` tracks the cutoff so we only push *new*
interactions on subsequent syncs — not the full history every time.

**Materialization into Trakt.** Rather than calling Recombee in-band
on every `/feeds` hit, we pre-materialize picks into two managed Trakt
lists. Trade-offs:

| Concern | Live call | Materialized |
| --- | --- | --- |
| `/feeds` latency budget (≤200ms) | +100–200ms | Zero extra calls |
| Recombee outage impact | Every `/feeds` fails | Previous recs remain valid |
| Chillio refreshes are frequent | Hammers Recombee | Cheap DB read |
| Users see recs in Trakt too | No | Yes — visible at trakt.tv |

**Graceful degradation.** If Recombee is unavailable (missing keys,
network down, rate limit), `get_recombee().available` returns `False`
and feeds #2 and #3 fall back to a TMDB `discover` query filtered by
the user's top genres. Chillio still gets a valid 22-feed response.

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
| LLM | Ollama (`llama3.2:3b`) |
| Frontend | Jinja2 + iOS-style CSS |
| Reverse proxy | Caddy (HTTPS) |
| Containers | Docker Compose |

</details>

---

## API reference

### ChillLink endpoints

All endpoints accept an optional `user_id` query parameter, which
Chillio forwards on every call so we can personalize the response.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/manifest` | Addon metadata |
| `GET` | `/feeds` | Personalized 22-feed response |

Both endpoints are designed to **never** return 5xx. On any failure
they fall back to TMDB-backed defaults so Chillio always gets a valid
response.

### Admin endpoints

Set `ADMIN_TOKEN` in `.env` to enable manual sync triggers and an
operational status view. When the token is blank (the default),
`/admin/*` returns `503` — the surface is off unless you turn it on.
All requests require the header `X-Admin-Token: <your token>`.

```bash
# Trigger a full content refresh (TMDB → embeddings → Recombee → catalog)
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
     https://<your-host>/admin/sync/content

# Re-sync a single user (taste profile + interactions + list refresh)
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
     https://<your-host>/admin/sync/user/<user_id>

# Operational snapshot: Recombee availability, user/content counts, last-sync timestamps
curl -H "X-Admin-Token: $ADMIN_TOKEN" \
     https://<your-host>/admin/status
```

The two `POST` routes return `202 Accepted` and run the job in the
background — tail `docker compose logs app` to watch progress.

---

## Background jobs

| Job | Schedule | Purpose |
| --- | --- | --- |
| `content_sync` | Daily 03:00 UTC | Pull popular/trending/top-rated TMDB titles → embed → ChromaDB + Recombee + catalog |
| `user_sync` | Every 4 hours | Re-build each user's taste profile, push interactions to Recombee, refresh managed Trakt lists |
| `token_refresh` | Every 6 hours | Refresh any Trakt token expiring within 48h |

Logs go to `docker compose logs app`.

---

## Troubleshooting

**Feeds return but Chillio shows nothing for a row.**
Rows with `source: trakt_list` depend on a managed list having items.
Wait for the first `user_sync` to complete (check logs). Rows with
`source: tmdb_query` always resolve via TMDB.

**`Trakt rejected the authorization`** on callback.
Double-check that the **Redirect URI** configured in your Trakt app
matches `BASE_URL + /auth/callback` exactly, including `https://`.

**OAuth state cookie rejected.**
Browsers require `Secure`+`HttpOnly` cookies over HTTPS. Make sure
`BASE_URL` is HTTPS in production and your Caddy cert is valid.

**Ollama keeps warning `model not found`.**
The first boot pulls `llama3.2:3b`. On slow links this can take 10+
min. Reclio works fine without Ollama — BYW titles fall back to
`f"Because You Watched {title}"`.

**Recombee recommendations are empty.**
Recombee needs at least a dozen items and interactions before it
produces useful recs. The initial `user_sync` seeds both.

**sentence-transformers fails on ARM.**
If building on Apple Silicon outside Docker, install via
`pip install sentence-transformers torch --extra-index-url https://download.pytorch.org/whl/cpu`.

---

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys
uvicorn app.main:app --reload --port 8000
```

For HTTPS during local OAuth testing, tunnel with
[`cloudflared`](https://github.com/cloudflare/cloudflared) or
[`ngrok`](https://ngrok.com/) and set `BASE_URL` to the tunnel URL.

---

## License

MIT.
