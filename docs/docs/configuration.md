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

## LLM provider (chat)

Reclio uses an LLM for row titles, the personality blurb, the Ask
Reclio chat, and the conversational preference flow. Pick one
provider — or leave it off entirely and the LLM-driven features fall
back gracefully (plain f-strings for titles, "chat offline" state).

| Variable | Default | Description |
| --- | --- | --- |
| `LLM_PROVIDER` | `ollama` | `ollama` · `claude` · `openai` · `openrouter` · `none` |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama HTTP endpoint |
| `OLLAMA_MODEL` | `llama3.2:3b` | Ollama chat model |
| `ANTHROPIC_API_KEY` | — | Required when `LLM_PROVIDER=claude` |
| `CLAUDE_MODEL` | `claude-haiku-4-5` | Anthropic model name |
| `OPENAI_API_KEY` | — | Required when `LLM_PROVIDER=openai` *or* when `EMBEDDING_PROVIDER=openai` |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI chat model |
| `OPENROUTER_API_KEY` | — | Required when `LLM_PROVIDER=openrouter` |
| `OPENROUTER_MODEL` | `anthropic/claude-3.5-haiku` | OpenRouter model in `vendor/model` form |

See [Model integrations](./model-integrations) for picking a provider
and the workload they actually serve.

## Embedding provider (vector similarity)

Independent of the chat LLM. Default `auto` follows `LLM_PROVIDER`
with sensible per-provider mappings (Ollama → nomic-embed-text,
OpenAI → text-embedding-3-small, Claude/OpenRouter → local
sentence-transformers MiniLM, none → null). Override for
mix-and-match — e.g. chat on Claude, embeddings on OpenAI for the
top-tier 1536d quality.

| Variable | Default | Description |
| --- | --- | --- |
| `EMBEDDING_PROVIDER` | `auto` | `auto` · `openai` · `ollama` · `local` · `none` |
| `OLLAMA_EMBEDDING_MODEL` | `nomic-embed-text` | Ollama embedding model name |

See [Embeddings](./embeddings) for the full quality/cost/footprint
comparison.

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

In v1.5 the user-sync sweep also runs an
[hourly health check](./api-reference#diagnostics-added-in-v15) on
every external dependency (DB, Trakt, TMDB, Recombee, LLM). It's
silent on the happy path and logs WARNING with full diagnostic detail
on any degradation.

See [Adaptive sync](./adaptive-sync) for the full sync-cadence model
and [Watch-state machine](./watch-state) for how Reclio learns from
incomplete watches.
