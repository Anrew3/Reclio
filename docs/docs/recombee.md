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
`get_recombee().available` returns `False` and feeds #2 and #3 fall
back to a TMDB `discover` query filtered by the user's top genres.
Chillio always gets a valid 22-feed response.
