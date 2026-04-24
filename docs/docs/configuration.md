---
id: configuration
title: Configuration
sidebar_position: 3
---

# Configuration

Every setting is an environment variable. The repo ships an
`.env.example` file — copy it to `.env` and fill in what you need.

## Required

| Variable | Description |
| --- | --- |
| `TRAKT_CLIENT_ID` | Trakt OAuth app client id |
| `TRAKT_CLIENT_SECRET` | Trakt OAuth app client secret |
| `TMDB_API_KEY` | TMDB v3 API key |
| `RECOMBEE_DATABASE_ID` | Recombee database id |
| `RECOMBEE_PRIVATE_TOKEN` | Recombee private token |
| `FERNET_KEY` | 32-byte base64 key used to encrypt Trakt tokens at rest |
| `SECRET_KEY` | Signing key for session cookies |
| `BASE_URL` | Public URL, no trailing slash (e.g. `https://reclio.example.com`) |

## Optional

| Variable | Default | Description |
| --- | --- | --- |
| `RECOMBEE_REGION` | `us-west` | `us-west` · `eu-west` · `ap-se` |
| `ADMIN_TOKEN` | _(blank = disabled)_ | Enables `/admin/*` endpoints when set |
| `PORT` | `8000` | Internal app port |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/db/reclio.db` | SQLAlchemy URL |
| `CHROMA_PERSIST_DIR` | `./data/chroma` | ChromaDB storage path |

## LLM provider

Reclio uses a small LLM for row titles like *"Because you watched
Interstellar"*. Pick one provider — or leave it off entirely and
feeds fall back to plain f-strings.

| Variable | Default | Description |
| --- | --- | --- |
| `LLM_PROVIDER` | `ollama` | `ollama` · `claude` · `openai` · `none` |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama HTTP endpoint |
| `OLLAMA_MODEL` | `llama3.2:3b` | Ollama model name |
| `ANTHROPIC_API_KEY` | — | Required when `LLM_PROVIDER=claude` |
| `CLAUDE_MODEL` | `claude-haiku-4-5` | Anthropic model name |
| `OPENAI_API_KEY` | — | Required when `LLM_PROVIDER=openai` |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI chat model |

See [Model integrations](./model-integrations) for details.

## Adaptive sync

Each user's taste profile re-syncs based on how active they are. The
defaults work well; override if you want tighter or looser intervals.

| Variable | Default | Description |
| --- | --- | --- |
| `USER_SYNC_DEFAULT_INTERVAL_HOURS` | `8` | Baseline per-user cadence |
| `USER_SYNC_HOT_INTERVAL_HOURS` | `4` | Cadence for "hot" users (frequent `/feeds` hits) |
| `USER_SYNC_COLD_INTERVAL_HOURS` | `24` | Cadence for "cold" users (rare `/feeds` hits) |
| `USER_SYNC_HOT_THRESHOLD_PER_WEEK` | `14` | ≥ this many hits in 7d → hot |
| `USER_SYNC_COLD_THRESHOLD_PER_WEEK` | `3` | ≤ this many hits in 7d → cold |
| `USER_SYNC_SWEEP_INTERVAL_HOURS` | `1` | How often the scheduler checks for stale users |
| `CONTENT_SYNC_INTERVAL_HOURS` | `24` | Global TMDB catalog refresh |
| `TOKEN_REFRESH_INTERVAL_HOURS` | `6` | Trakt OAuth token refresh |

See [Adaptive sync](./adaptive-sync) for the full model.
