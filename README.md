<div align="center">

# Reclio

**Netflix-style personalized recommendations for [Chillio](https://chillio.app), powered by your Trakt.**

[![Python](https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![ChillLink Protocol](https://img.shields.io/badge/ChillLink-Protocol-7B5CFF)](https://chillio.app)
[![CI](https://img.shields.io/github/actions/workflow/status/Anrew3/reclio/ci.yml?branch=main&label=CI)](https://github.com/Anrew3/reclio/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-16A34A)](#license)

### [Try it now → **reclio.p0xl.com**](https://reclio.p0xl.com)

_Connect your Trakt. Copy your addon URL. Paste into Chillio. Done._

</div>

---

## What it is

Reclio reads your Trakt watch history and turns it into 10 personalized
rows for [Chillio](https://chillio.app) — *Recommended For You*,
*Because You Watched*, *Trending*, *Top Genre You'll Love*, and
*Hidden Gems*, each split into a movies + shows pair.

Behind those rows: **Recombee** collaborative filtering, **vector
embeddings** for semantic similarity, a **watch-state machine** that
learns from incomplete watches, an LLM-driven **conversational
chat** ("stop showing me horror" → it actually does), and a
recency-weighted **personality blurb** that gently roasts your taste.

Use the public instance at [**reclio.p0xl.com**](https://reclio.p0xl.com)
or self-host with one `docker compose up`.

## What you get

- **10 personalized rows** (5 movie + 5 show) that learn from what you watch *and* what you abandon
- **Trakt-native** — watches, ratings, watchlist, *and incomplete starts* all factor in
- **Ask Reclio** — floating chat bubble that can answer "why am I seeing X" and act on "stop showing me Y"
- **Personality card** — iOS Health-style donut chart of your top genres + an LLM-written one-line roast
- **Adaptive sync** — heavy users refresh hourly, dormant users weekly, ~80% less Trakt API traffic than v1.4
- **Always works** — every external service can fail and you still get recs

---

## Use it (5 steps)

1. Open **[reclio.p0xl.com](https://reclio.p0xl.com)** in any browser
2. **Connect with Trakt** and authorize
3. Tap **Copy** on your personal addon URL
4. In Chillio: **Settings → ChillLink Servers → Add Server** → paste → Save
5. Pull-to-refresh the home tab. The 10 rows appear.

First-time users see mostly generic feeds for ~2 minutes while the
taste profile builds, then refresh.

---

## Self-host

You need Docker, a domain, and four free API keys (Trakt, TMDB,
Recombee, optionally an LLM provider). The short version:

```bash
git clone https://github.com/Anrew3/reclio.git && cd reclio
cp .env.example .env       # then fill in the keys
docker compose up -d --build
```

Full guide — keys, env vars, Caddy vs Traefik, troubleshooting — lives
in the docs.

---

## Documentation

Full docs at **[anrew3.github.io/Reclio](https://anrew3.github.io/Reclio/)**
(auto-deployed from `main`). Source under [`docs/`](docs/).

| Section | What's there |
| --- | --- |
| [Setup](https://anrew3.github.io/Reclio/setup) | Docker Compose + local dev + getting API keys |
| [Configuration](https://anrew3.github.io/Reclio/configuration) | Every env var, every default |
| [Model integrations](https://anrew3.github.io/Reclio/model-integrations) | Ollama · Claude · OpenAI · OpenRouter |
| [Embeddings](https://anrew3.github.io/Reclio/embeddings) | What they are, why we use them, provider tradeoffs |
| [Recombee](https://anrew3.github.io/Reclio/recombee) | Collaborative-filtering deep-dive |
| [Watch-state machine](https://anrew3.github.io/Reclio/watch-state) | How Reclio learns from incomplete watches |
| [API reference](https://anrew3.github.io/Reclio/api-reference) | `/manifest`, `/feeds`, `/admin/*` |
| [Adaptive sync](https://anrew3.github.io/Reclio/adaptive-sync) | How sync cadence auto-tunes |
| [Caddy vs Traefik](https://anrew3.github.io/Reclio/caddy-vs-traefik) | Reverse-proxy trade-offs |
| [Troubleshooting](https://anrew3.github.io/Reclio/troubleshooting) | Common gotchas |

---

## License

MIT.
