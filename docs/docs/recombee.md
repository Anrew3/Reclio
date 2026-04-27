---
id: recombee
title: Recombee
sidebar_position: 5
---

# Recombee integration

[Recombee](https://www.recombee.com/) powers the two "Recommended For
You" rows (feeds #2 and #3). Everything else is derived directly from
Trakt or TMDB — Recombee handles the collaborative filtering that
can't be computed from a single user's history alone.

## Data flow

```
  ┌─────────────┐   daily     ┌──────────┐  items   ┌──────────┐
  │    TMDB     │────────────▶│  Reclio  │─────────▶│ Recombee │
  └─────────────┘             │ content  │          │ catalog  │
                              │  sync    │          └─────┬────┘
                              └──────────┘                │
                                                          │ train
  ┌─────────────┐  adaptive   ┌──────────┐ interactions   │
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

## Catalog properties

Per-title properties pushed to Recombee from
[`content_sync`](https://github.com/Anrew3/reclio/blob/main/app/jobs/content_sync.py):

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

## User interactions

| Trakt signal | Recombee call | Notes |
| --- | --- | --- |
| Watch history item | `AddDetailView(user, item, ts)` | Implicit positive signal |
| Rating (1–10) | `AddRating(user, item, score, ts)` | Normalized to `[-1.0, 1.0]`; Trakt 5.5 ≈ 0, 10 → +1.0, 1 → −1.0 |
| Watchlist entry | `AddBookmark(user, item)` | Strong positive intent signal |

`users.last_history_sync` tracks the cutoff so we only push *new*
interactions on subsequent syncs.

## Why materialize into Trakt lists?

Rather than calling Recombee on every `/feeds` hit, we pre-materialize
picks into two managed Trakt lists (`Reclio • Recommended Movies`,
`Reclio • Recommended Shows`).

| Concern | Live call | Materialized |
| --- | --- | --- |
| `/feeds` latency budget (≤200ms) | +100–200ms | Zero extra calls |
| Recombee outage impact | Every `/feeds` fails | Previous recs still work |
| Chillio refreshes are frequent | Hammers Recombee | Cheap DB read |
| Users see recs in Trakt too | No | Yes — browsable at trakt.tv |

## Graceful degradation

If Recombee is unavailable (missing keys, network down, rate limit),
`get_recombee().available` returns `False` and the *Recommended For You*
rows fall back to a TMDB `discover` query filtered by the user's top
genres. The *Because You Watched* rows, when their managed Trakt list
is empty, fall back to TMDB `/movie/{id}/recommendations` for the
last-watched item — and on cold-start, the [vector similarity
service](./embeddings) seeds these rows from semantic neighbors.
Chillio always gets a valid 10-feed response.

## Recombee vs embeddings — which signal does what?

Reclio uses both **collaborative filtering** (Recombee) and **content-based
embeddings** because they answer fundamentally different questions:

| Signal | Question | Strength | Weakness |
|---|---|---|---|
| **Recombee** | "What does this *user* want next?" | Best when the user has 12+ Trakt interactions; learns from collective behavior across all users | Cold-starts users badly; cold-starts new films invisibly |
| **Embeddings** | "What is similar to *this film*?" | Always works (no user data needed); catches semantic vibe genre tags miss | Doesn't know what *you* like, only what looks like X |

The *Because You Watched* row is a 60/40 weighted blend of Recombee
item-to-item + vector neighbors so it gets the best of both: Recombee's
collective-behavior signal leads, embeddings fill in semantic neighbors
Recombee's co-watch graph hasn't seen yet.

For new users (`<5` Recombee results) the *Recommended For You* row
falls back to vector neighbors of the user's highest-rated Trakt
title so the home screen feels personalized from day one.

## Diagnosing why nothing shows up in the Recombee web UI

The most common silent-failure mode is `wrong_region` — Reclio's
local catalog is full of items, `recombee_synced=true` on every row,
but the Recombee web UI shows zero items. New diagnostic endpoint
detects exactly this:

```bash
curl -H "X-Admin-Token: T" https://<your-host>/admin/recombee/diagnose | jq .verdict
```

Returns one of: `ok` / `wrong_region` / `unreachable` /
`no_pushes_yet` / `writes_silently_failing` / `no_credentials` /
`sdk_missing` / `client_init_failed`. Each carries `next_steps`
in plain English. See [Troubleshooting](./troubleshooting) for the
full remediation matrix.

The hourly background sanity check ([health-check job](./watch-state)
runs the same probe and logs a single WARNING with the verdict if
anything degrades.
