---
id: api-reference
title: API reference
sidebar_position: 6
---

# API reference

## ChillLink endpoints

Both endpoints are designed to **never** return 5xx. On any failure
they fall back to TMDB-backed defaults so Chillio always gets a valid
response.

### `GET /manifest`

Addon metadata. Shape is protocol-fixed.

```json
{
  "id": "reclio-recommendations",
  "version": "1.0.0",
  "name": "Reclio",
  "description": "Netflix-style personalized recommendations powered by your Trakt history",
  "supported_endpoints": { "feeds": "/feeds", "streams": null }
}
```

### `GET /feeds`

Returns the **10-feed personalized response** (5 movie + 5 show pairs):

| Position | ID prefix | Title | Source |
| --- | --- | --- | --- |
| 1 / 2 | `recommended_*` | Recommended For You | Recombee → Trakt managed list (cold-start: vector seed) |
| 3 / 4 | `because_watched_*_tmdb_*` | Because You Watched [last] | Recombee item-to-item + vector blend → Trakt list |
| 5 / 6 | `trending_*` | Trending Movies / Shows | TMDB `/trending` |
| 7 / 8 | `top_genre_*` | [Genre] Movies / Shows You'll Love | TMDB `/discover` (your top genre + prefs) |
| 9 / 10 | `hidden_gems_*` | Hidden Gem Movies / Shows | TMDB `/discover` (vote_average ≥ 7.5, scaled by `discovery_level`) |

| Query param | Type | Purpose |
| --- | --- | --- |
| `user_id` | string | Reclio member UUID. Primary personalization key. |
| `username` | string | Trakt username. Fallback when `user_id` isn't set yet (e.g. first install on a second device). |
| `session_id` | string | Opaque Chillio client identifier. Logged for per-device analytics; does not affect the response. |
| `last_watched` | string | Live hint in the form `movie:<tmdb_id>` or `show:<tmdb_id>`. Overrides the latest-watch used for BYW rows without waiting for the next Trakt sync. |

Example:

```
GET /feeds?user_id=abc123&session_id=iphone-home&last_watched=movie:157336
```

Response shape:

```json
{
  "feeds": [
    {
      "id": "because_watched_movie_tmdb_157336",
      "title": "Because you watched Interstellar",
      "source": "trakt_list",
      "source_metadata": { "list_id": "reclio-byw-abc123" },
      "content_type": "movies"
    },
    ...
  ]
}
```

**Feed IDs are stable** but carry enough signal for Chillio's cache to
invalidate correctly. BYW rows include the TMDB id of the watched
title (`because_watched_movie_tmdb_157336`), so if the user's latest
watch changes, Chillio sees it as a new feed rather than silently
changing the title of an existing one.

## Admin endpoints

Set `ADMIN_TOKEN` to enable these. When the token is blank (the
default), `/admin/*` returns `503`. All requests require the header
`X-Admin-Token: <your token>`.

```bash
# Full content refresh (TMDB → embeddings → Recombee → catalog)
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
     https://<your-host>/admin/sync/content

# Re-sync a single user
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
     https://<your-host>/admin/sync/user/<user_id>

# Operational snapshot
curl -H "X-Admin-Token: $ADMIN_TOKEN" \
     https://<your-host>/admin/status
```

The two `POST` routes return `202 Accepted` and run the job in the
background — tail `docker compose logs app` to watch progress.

`/admin/status` returns counts of accounts, members, catalog items,
plus Recombee availability and the timestamp of the last content sync.

### Diagnostics (added in v1.5)

```bash
# Recombee end-to-end health probe (verdict + remediation)
curl -H "X-Admin-Token: $ADMIN_TOKEN" \
     https://<your-host>/admin/recombee/diagnose

# Optional: add ?write_test=1 to round-trip a tiny test item
curl -H "X-Admin-Token: $ADMIN_TOKEN" \
     "https://<your-host>/admin/recombee/diagnose?write_test=1"

# Recombee preview — what's the rec engine actually producing for a user?
curl -H "X-Admin-Token: $ADMIN_TOKEN" \
     https://<your-host>/admin/recombee/preview/<user_id>?count=10

# Hourly health-check buffer — last 24 snapshots of DB / Trakt / TMDB / Recombee / LLM
curl -H "X-Admin-Token: $ADMIN_TOKEN" \
     https://<your-host>/admin/health/history

# Trigger a health snapshot immediately (lands in /admin/health/history within ~10s)
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
     https://<your-host>/admin/health/run

# Watch-state inspection — open WatchAttempts grouped by verdict
curl -H "X-Admin-Token: $ADMIN_TOKEN" \
     https://<your-host>/admin/watch_attempts/<user_id>

# Vector-similarity neighbors for any catalog item (debugging embeddings)
curl -H "X-Admin-Token: $ADMIN_TOKEN" \
     https://<your-host>/admin/similar/movie_27205?k=12
```

`/admin/recombee/diagnose` is the most useful when something looks
off in the Recombee web UI. It returns one of:
`ok` / `wrong_region` / `unreachable` / `no_pushes_yet` /
`writes_silently_failing` / `no_credentials` plus plain-English
`next_steps`. See [Troubleshooting](./troubleshooting) for full
remediation matrix.
