---
id: recombee
title: Recommendation engine
sidebar_position: 5
---

# Recommendation engine

Since v1.7 Reclio ships its own **local recommendation engine** — the
default backend, and the reason the only required API keys are Trakt
and TMDB. The pre-1.7 [Recombee](https://www.recombee.com/) SaaS
integration remains available as an opt-in legacy backend.

Pick the backend with one env var:

```bash
RECOMMENDER=local      # default — fully self-hosted
RECOMMENDER=recombee   # legacy — proxy to the Recombee SaaS
```

## How the local engine works

Everything lives in Reclio's own SQLite database. Two tables matter:

- **`interactions`** — one row per (user, item, kind) with a signed
  weight. Kinds: `view` (watch history), `rating` (Trakt rating,
  normalized to −1…+1), `bookmark` (watchlist), `signal` (watch-state
  verdicts like *completed* +0.5 or *S1E1 bounce* −1.0), and `block`
  ("never show me this" from Ask Reclio).
- **`content_catalog`** — TMDB metadata plus a vector embedding per
  title (see [Embeddings](./embeddings)). The daily content sync grows
  it from TMDB's popular/top-rated/trending lists **and** from
  genre-targeted sweeps around the instance's actual taste profiles;
  user sync backfills (and embeds) anything from your history the
  sweeps missed.

`item_id` is always `movie_{tmdb_id}` or `tv_{tmdb_id}` — the type
prefix prevents collisions between movies and shows that share a TMDB
numeric id.

### Recommended For You

```
profile = Σ  kind_weight × recency_decay(when) × embedding(item)
score   = cosine(profile, item) + 0.15 × popularity_norm(item)
```

Your profile vector is the recency-weighted mean of the embeddings of
everything you've interacted with — positive weights pull the ranking
toward what you finish and rate up; negative weights (bounces, blocks,
low ratings) actively push it away from similar titles. Items you've
watched or blocked are excluded outright. A small popularity prior
keeps the row from drifting into obscurities, and a nine-month
half-life makes the row chase your current mood rather than your
2016 self.

With no usable history (cold start) the engine falls back to a
quality-floored popularity ranking over the catalog, so the row is
never empty.

### Because You Watched

Pure semantic neighbors of your most recently finished title, from the
same embedding matrix, minus everything you've seen or blocked. The
whole catalog scores in a single ~30 ms NumPy matrix multiply.

### Why local wins for self-hosting

Collaborative filtering needs *many* users' histories to overlap
before it beats content-based ranking. A self-hosted instance has a
handful of users, so a SaaS recommender adds an account, two secrets,
a region pitfall, and a network dependency — for signal you can mostly
compute locally. The local engine also sees richer feedback than
Recombee ever did: watch-state verdicts feed it directly instead of
being flattened into a rating call.

## Why materialize into Trakt lists?

Rather than ranking on every `/feeds` hit, user sync pre-materializes
picks into managed Trakt lists (`Reclio • Recommended Movies`,
`Reclio • Recommended Shows`, plus the two *Because You Watched*
lists).

| Concern | Live call | Materialized |
| --- | --- | --- |
| `/feeds` latency budget (≤200ms) | ranking on every hit | zero extra work |
| Sync outage impact | every `/feeds` degrades | previous recs still serve |
| Chillio refreshes are frequent | recompute each time | cheap list read |
| Users see recs in Trakt too | no | yes — browsable at trakt.tv |

## Graceful degradation

If the engine has nothing to say (fresh install, embeddings disabled),
the *Recommended For You* rows fall back to a TMDB `discover` query
filtered by the user's top genres, and *Because You Watched* falls
back to TMDB `/movie/{id}/recommendations` for the last-watched item.
Chillio always gets a valid 10-feed response.

## Inspecting the engine

```bash
# Raw ranked IDs per media type for a user
curl -H "X-Admin-Token: T" https://<host>/admin/engine/preview/<user_id>

# Engine probe (interaction store + end-to-end ranking) among all probes
curl -H "X-Admin-Token: T" https://<host>/admin/selftest | jq '.probes[] | select(.name=="engine")'

# Quick counts (catalog / embedded / interactions)
curl https://<host>/health | jq .checks.engine
```

## Legacy: Recombee mode

Set `RECOMMENDER=recombee` and install the SDK (no longer bundled):

```bash
pip install recombee-api-client
```

then provide:

```bash
RECOMBEE_DATABASE_ID=...
RECOMBEE_PRIVATE_TOKEN=...   # the *private* token, not the public one
RECOMBEE_REGION=us-west      # must match your Recombee web UI URL
```

Reclio then mirrors the catalog and interactions to Recombee and asks
it for `RecommendItemsToUser` / `RecommendItemsToItem`, exactly as
pre-1.7 versions did. Interactions are **also** recorded locally, so
you can switch back to `RECOMMENDER=local` at any time without losing
history.

`GET /admin/recombee/diagnose` remains available in this mode and
returns one of: `ok` / `wrong_region` / `unreachable` /
`no_pushes_yet` / `writes_silently_failing` / `no_credentials` /
`sdk_missing` / `client_init_failed`, each with plain-English
`next_steps`. See [Troubleshooting](./troubleshooting) for the full
remediation matrix.
