---
id: intro
title: Introduction
sidebar_position: 1
slug: /
---

# Reclio

**Netflix-style personalized recommendations for [Chillio](https://chillio.app), powered by your Trakt.**

Reclio is a ChillLink Protocol addon server. It reads your Trakt watch
history (and what you *don't* finish), builds a taste profile, and
returns **10 personalized rows** — five movie + five show pairs — to
Chillio every time the app opens.

```
Trakt history    ─┐
Ratings          ─┤
Watchlist        ─┼─▶  Reclio  ─▶  Chillio (/feeds — 10 rows)
Watch progress   ─┤    │
Watched %        ─┘    ├─ Recombee (collaborative filtering)
                       ├─ Vector embeddings (semantic similarity)
                       ├─ Watch-state machine (incomplete-watch signal)
                       └─ LLM (chat, personality, intent classification)
```

## What you get

- **10 personalized rows** — *Recommended For You*, *Because You Watched*,
  *Trending*, *Top Genre You'll Love*, and *Hidden Gems*, each as a
  movies + shows pair. Drops the v1.4 grab-bag of 22 sections in
  favor of a tight, repeatable layout.
- **Trakt-native + watch-state aware** — learns from watches, ratings,
  watchlist, *and from incomplete watches* (the v1.5 watch-state
  machine catches "fell asleep" vs "actively bounced" and signals
  Recombee accordingly).
- **Ask Reclio chat** — floating bubble on the dashboard. Ask it
  anything ("why am I seeing X?", "more thrillers please", "stop
  recommending horror") and it can actually act on the answer.
- **Personality card** — iOS Health-style donut chart of your top
  genres + an LLM-written one-line roast.
- **Self-hostable** — one `docker compose up` and you have your own
  instance with Caddy-managed HTTPS.
- **Never 5xx** — every external service (Trakt, TMDB, Recombee, LLM,
  embeddings) can fail independently and `/feeds` still returns a
  valid response.

## Two ways to use it

1. **Public instance** — open
   [reclio.p0xl.com](https://reclio.p0xl.com), connect Trakt, paste
   your addon URL into Chillio. No installation.
2. **Self-host** — clone the repo, fill in four API keys, run
   `docker compose up -d`. See [Setup](./setup).

## Where to go next

- [Setup](./setup) — running Reclio locally or in Docker
- [Configuration](./configuration) — every env var explained
- [Model integrations](./model-integrations) — picking a chat LLM
  (Ollama, Claude, OpenAI, OpenRouter)
- [Embeddings](./embeddings) — what they are, why we use them, how to
  pick a provider
- [Recombee](./recombee) — how the collaborative filtering works
- [Watch-state machine](./watch-state) — how Reclio learns from
  incomplete watches
- [API reference](./api-reference) — `/manifest`, `/feeds`, `/admin/*`
- [Adaptive sync](./adaptive-sync) — how sync cadence auto-tunes
- [Caddy vs Traefik](./caddy-vs-traefik) — picking a reverse proxy
- [Troubleshooting](./troubleshooting) — common gotchas
