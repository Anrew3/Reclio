---
id: embeddings
title: Embeddings
sidebar_position: 5
---

# Embeddings

Embeddings are how Reclio answers "find me films that *feel* like this
one" — semantic similarity that goes beyond what TMDB's genre tags can
capture.

## What an embedding actually is

An embedding is a list of numbers — a **vector** — that represents the
meaning of a piece of text. For each film in our catalog, Reclio
generates one vector from the title + tagline + overview + genre names
+ top keywords. Two films whose meaning is similar end up with vectors
that point in similar directions; two films with nothing in common
point in different directions.

We compare them with **cosine similarity** — a single number between
-1 and 1 saying how aligned the vectors are. *Memento* and *Mulholland
Drive* share zero TMDB genre IDs, but their embeddings cluster
together because the model learned during pre-training that "non-linear
narrative + identity + memory" is a recurring vibe. Genre tags can't
represent that. Embeddings can.

```
"Inception. A thief who steals corporate secrets through dream-sharing
 technology... Genres: Action, Sci-Fi, Adventure. Keywords: dream,
 heist, subconscious, memory."
                    │
                    ▼  embed_text(...)
        [0.0271, -0.0128, 0.0843, ..., -0.0456]   # 1536 numbers (with text-embedding-3-small)
                    │
                    ▼  cosine similarity vs every other catalog item
        Top neighbors: Tenet, Shutter Island, Coherence, Primer,
                       Source Code, The Prestige, Donnie Darko, ...
```

## Where Reclio uses them

Three rails depend on embeddings:

| Row | How embeddings help |
|---|---|
| **Because You Watched** | 60/40 blend of Recombee item-to-item + vector neighbors. Recombee leads (collective behavior signal), embeddings fill in semantic neighbors Recombee's co-watch graph hasn't picked up yet. |
| **Recommended For You** (cold-start) | When Recombee returns &lt; 5 items (new user), seed the row from vector neighbors of the user's highest-rated film. |
| `/admin/similar/<id>` | Direct sanity check — given a TMDB ID, what does the embedding model think is closest? Useful for verifying the catalog is healthy. |

## Why we have *both* Recombee and embeddings

They answer **fundamentally different questions** — see
[Recombee](./recombee) for the deep dive — but the short version:

- **Recombee** answers *"what does **this user** want next?"* (collaborative
  filtering — learns from collective behavior).
- **Embeddings** answer *"what is similar to **this film**?"* (content-based
  — looks only at the film, ignores the user).

Each has a glaring weakness:

- Content-based filtering creates a **filter bubble**. You like noir,
  you only ever see noir. The system never surprises you.
- Collaborative filtering has a **cold-start problem**. New films with
  12 viewers are invisible. New users with 8 watched titles get
  random recs.

Combined &gt; either alone. This is why every serious recommendation
system (Netflix, Spotify, YouTube) uses both. Reclio does too.

## Picking a provider

The `EMBEDDING_PROVIDER` env var picks which model generates the
vectors. Default `auto` follows whatever you set for `LLM_PROVIDER`
with sensible per-provider mappings:

| `LLM_PROVIDER` | Auto-resolved embedding model | Dim |
|---|---|---|
| `ollama` | Ollama `nomic-embed-text` | 768 |
| `openai` | OpenAI `text-embedding-3-small` | 1536 |
| `claude` | local `sentence-transformers/all-MiniLM-L6-v2` | 384 |
| `openrouter` | local `sentence-transformers/all-MiniLM-L6-v2` | 384 |
| `none` | NullProvider (similarity rail empty) | — |

Set `EMBEDDING_PROVIDER` explicitly to override. The most common
reason: chat on Claude/OpenRouter for variety, embeddings on OpenAI
for top-tier 1536d quality.

```bash
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-...

EMBEDDING_PROVIDER=openai          # ← override
OPENAI_API_KEY=sk-...              # only used for embeddings
```

Allowed `EMBEDDING_PROVIDER` values: `auto` (default) · `openai` ·
`ollama` · `local` (sentence-transformers) · `none`.

## Quality / cost / footprint comparison

Sorted by what would actually improve Reclio's recommendations:

| Model | Dim | Source | Footprint | Cost (~10K item catalog backfill) |
|---|---|---|---|---|
| OpenAI `text-embedding-3-large` | 3072 | hosted | network | ~$1.30 |
| **OpenAI `text-embedding-3-small`** ★ | 1536 | hosted | network | **~$0.20** |
| Voyage `voyage-3-large` | 1024 | hosted | network | ~$1.80 |
| Voyage `voyage-3` | 1024 | hosted | network | ~$0.60 |
| Cohere `embed-english-v3.0` | 1024 | hosted | network | ~$1.00 |
| `mxbai-embed-large-v1` | 1024 | self-host | ~700 MB resident | free |
| `BAAI/bge-large-en-v1.5` | 1024 | self-host | ~1.3 GB resident | free |
| **Ollama `nomic-embed-text`** | 768 | self-host (Ollama) | ~280 MB resident | free |
| `all-mpnet-base-v2` | 768 | self-host | ~400 MB resident | free |
| **`all-MiniLM-L6-v2`** ← default fallback | 384 | self-host | ~250 MB resident | free |

★ = recommended for most installs.

For movie metadata specifically, the practical quality ranking is:

```
text-embedding-3-large ≈ voyage-3-large
  > text-embedding-3-small ≈ voyage-3
  > mxbai-embed-large > bge-large > nomic-embed
  > mpnet > MiniLM
```

The gaps narrow as you go right. The biggest single jump is
**MiniLM → text-embedding-3-small**. Past that, you're paying real
money for real diminishing returns.

## "Higher dim is always better" is a myth

| Dim | What it captures |
|---|---|
| 384 (MiniLM) | Broad themes well. Loses nuance on long, dense descriptions. |
| 768 (nomic / mpnet) | Sweet spot for self-hosted. Distinguishes prestige drama from lit-adaptation drama, neo-noir from straight noir. |
| 1536 (OpenAI -small) | Noticeably sharper on subtle differences. Best practical quality/cost ratio. |
| 3072 (OpenAI -large) | Marginal returns over 1536d for our content type. Mostly matters in academic benchmarks. |

Higher dimensions = bigger storage + slower queries. Not always worth it.

## Footprint by provider

Catalog of 10K items with each provider:

| Provider | Per-item bytes | Total | RAM with model loaded |
|---|---|---|---|
| OpenAI text-embedding-3-small | 6 KB (1536 × 4) | ~60 MB | n/a (hosted) |
| Ollama nomic-embed-text | 3 KB (768 × 4) | ~30 MB | varies (Ollama process) |
| Local MiniLM | 1.5 KB (384 × 4) | ~15 MB | +250 MB (model) |

All three are negligible on any modern host.

## How re-embedding works

Embeddings get computed inside the daily `content_sync` job. A `source_hash`
column on `content_catalog` records the SHA-256 of the input text — if
TMDB updates a film's overview, the hash changes, the row is re-embedded
on the next sync. Items whose source hasn't changed are skipped (cheap,
no API call).

When you change `EMBEDDING_PROVIDER` (e.g. switching from MiniLM to
OpenAI), all existing embeddings have a different `embedding_model`
field. The similarity service detects the dimension mismatch and uses
the most common dim across the catalog, dropping outliers. Eventually
all rows get re-embedded by the daily sync.

To force a faster re-embed across the whole catalog: trigger the
sync manually via `POST /admin/sync/content` after changing the
provider.

## Common pitfalls

**"My catalog is in MiniLM but I changed `EMBEDDING_PROVIDER` to
`openai` — why isn't anything different?"** Embeddings are stored
per-item; switching the provider only affects items embedded *after*
the change. Trigger a full content sync (`POST /admin/sync/content`)
or wait for the daily 03:00 cron.

**"OpenRouter is set as my LLM but embeddings still use sentence-transformers."**
Correct — OpenRouter doesn't proxy embeddings, only chat completions.
Set `EMBEDDING_PROVIDER=openai` (with `OPENAI_API_KEY`) to use OpenAI
embeddings independently of your chat provider.

**"Ollama is set as my LLM and embeddings worked, then they stopped."**
The embedding model needs to be pulled separately from the chat
model: `docker compose exec reclio-ollama ollama pull nomic-embed-text`.
The hourly health check will log a WARNING if Ollama embeddings start
failing.

**"Should I use text-embedding-3-large?"** For Reclio specifically,
no — the marginal quality gain over `-small` is barely visible in
movie-recommendation use, and you're paying ~6× more. Stick with
`-small`.

## Future: LLM-as-reranker

The next dramatic improvement to recommendation quality won't come
from a bigger embedding model — it'll come from using the LLM as a
reranker at the very end of the pipeline. Take the top-30 candidates
from each row (Recombee + vector blend), then ask the LLM "given this
viewer's recent watches and personality, rank these 30 from most to
least likely to satisfy them." This is what frontier production
systems (Netflix, Spotify) do.

It's a v1.6+ feature. Embeddings will still do the candidate-
generation work; the LLM just refines the order.
