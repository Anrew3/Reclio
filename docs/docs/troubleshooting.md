---
id: troubleshooting
title: Troubleshooting
sidebar_position: 8
---

# Troubleshooting

Most issues fall into one of these buckets. If yours doesn't, check
`docker compose logs -f reclio` first — Reclio logs every external API
failure with enough detail to diagnose.

## Rows with `source: trakt_list` are empty

The first `user_sync` hasn't completed yet. Wait 30–60 seconds after
connecting Trakt, then pull-to-refresh. Rows with `source: tmdb_query`
always resolve via TMDB so they fill in immediately.

If the rows stay empty after several minutes, check
`/admin/recombee/preview/<user_id>` to confirm Recombee is producing
recommendations for that user. Empty results usually mean the user's
Trakt history isn't large enough yet for collaborative filtering — a
dozen-plus watched titles is the rough floor.

## "Trakt rejected the authorization"

The **Redirect URI** on your Trakt application must match
`BASE_URL + /auth/callback` **exactly**, including the `https://`
scheme. A trailing slash, a typo in the host, or `http://` instead of
`https://` will all trigger this.

## OAuth state cookie rejected

The OAuth state cookie is `Secure` + `HttpOnly` and only sets over
HTTPS. In production, `BASE_URL` must be `https://...` and Caddy /
Traefik must serve a valid certificate.

For local development, tunnel through `cloudflared` or `ngrok` and
point `BASE_URL` at the tunnel URL.

## Ollama keeps warning `model not found`

First boot pulls the model lazily. On slow connections this can take
10+ minutes. The app works fine while it pulls — Because-You-Watched
titles fall back to plain f-strings instead of LLM-generated variants.

To pre-pull the model and skip the wait:

```bash
docker compose exec reclio-ollama ollama pull llama3.2:3b
```

## Recombee recommendations are empty

Recombee needs both:

- **Items in the catalog** — wait for the first `content_sync` to push
  TMDB titles. Check `/admin/status` → `content.recombee_synced`.
- **User interactions** — wait for the first `user_sync` to push the
  user's Trakt history. A dozen-plus interactions is the practical floor.

If both look healthy and recs are still empty, hit
`/admin/recombee/preview/<user_id>` for the raw output.

## `/health` reports `degraded: true`

Open the response body — each downstream service has its own check
with status code or error string. The container stays "healthy" (HTTP
200) for everything except a database failure, because Trakt / TMDB /
Recombee / LLM all have graceful-degradation paths inside the app.

| Failed check | What still works |
| --- | --- |
| `trakt` | Cached watch history, materialized rec lists |
| `tmdb` | Cached titles (6h TTL), `trakt_list` rows |
| `recombee` | Feeds 2–3 fall back to TMDB `discover` queries |
| `llm` | BYW row titles fall back to f-strings |
| `db` | **Container is restarted by Docker** |
