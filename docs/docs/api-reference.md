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

Returns the 22-feed personalized response.

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
