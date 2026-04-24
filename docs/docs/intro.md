---
id: intro
title: Introduction
sidebar_position: 1
slug: /intro
---

# Reclio

**Netflix-style personalized recommendations for [Chillio](https://chillio.app), powered by your Trakt.**

Reclio is a ChillLink Protocol addon server. It reads your Trakt watch
history, builds a taste profile, and returns 22 personalized feeds to
Chillio every time the app opens.

```
Trakt history   ─┐
Ratings         ─┼─▶  Reclio  ─▶  Chillio (/feeds)
Watchlist       ─┘
```

## What you get

- **22 personalized rows** — *Because You Watched*, hidden gems,
  collaborative recs, decade throwbacks, watch progress, trending,
  and more.
- **Trakt-native** — learns from watches, ratings, and watchlist.
- **Self-hostable** — one `docker compose up` and you have your own
  instance with Caddy-managed HTTPS.
- **Never 5xx** — every external service (Trakt, TMDB, Recombee, LLM)
  can fail independently and `/feeds` still returns a valid response.

## Two ways to use it

1. **Public instance** — open
   [reclio.p0xl.com](https://reclio.p0xl.com), connect Trakt, paste
   your addon URL into Chillio. No installation.
2. **Self-host** — clone the repo, fill in four API keys, run
   `docker compose up -d`. See [Setup](./setup).

## Where to go next

- [Setup](./setup) — running Reclio locally or in Docker
- [Configuration](./configuration) — every env var explained
- [Model integrations](./model-integrations) — picking a LLM provider
- [Recombee](./recombee) — how the collaborative filtering works
- [API reference](./api-reference) — `/manifest`, `/feeds`, `/admin/*`
- [Adaptive sync](./adaptive-sync) — how sync cadence auto-tunes
- [Caddy vs Traefik](./caddy-vs-traefik) — picking a reverse proxy
