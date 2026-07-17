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
  verdicts like *completed* +0.5 or *S1E1 bounce* −1.0), `feedback`
  (reactions/comments from the /recommendations page), and `block`
  ("never show me this").
- **`content_catalog`** — TMDB metadata plus a vector embedding per
  title (see [Embeddings](./embeddings)). The daily content sync grows
  it from TMDB's popular/top-rated/trending lists **and** from
  genre-targeted sweeps around the instance's actual taste profiles;
  user sync backfills (and embeds) anything from your history the
  sweeps missed.

`item_id` is always `movie_{tmdb_id}` or `tv_{tmdb_id}` — the type
prefix prevents collisions between movies and shows that share a TMDB
numeric id.

### The ranking pipeline (v1.8)

**1. Taste facets.** Your positively-weighted items (and positive
comments — see below) are k-means clustered into up to 4 facets, each
a recency-weighted mean embedding. Every catalog item scores against
its *nearest* facet:

```
score = max_f cos(facet_f, item) − 0.35 × max(0, cos(negative, item))
        + 0.15 × popularity_norm − serve_decay
```

A viewer who loves both quiet dramas and loud action gets strong picks
near *both* poles instead of mush at the midpoint. Negative signal
(bounces, blocks, low ratings, critical comments) forms a separate
repulsion vector. A nine-month half-life makes the profile chase your
current mood rather than your 2016 self.

**2. Priors and decay.** A small popularity prior keeps the ranking
out of the obscurity tail, and items the engine has served repeatedly
without engagement decay a little more on every serve, so the row
rotates instead of going stale.

**3. MMR diversity re-rank.** The final list is assembled greedily
with maximal marginal relevance — each pick is penalized by its
similarity to already-picked items. Your `discovery_level` preference
drives the diversity weight.

**4. Preference sliders.** Eleven 0–100 sliders on the /preferences
page tune the pipeline directly: *Spotlight* scales the popularity
prior, *Acclaim* adds a vote-average quality prior, *Memory* sets the
profile's recency half-life (60–730 days), and four semantic sliders —
*Tone*, *Intensity*, *Brainpower*, *Humor* — embed their pole
descriptions ("dark gritty…" vs "light feel-good…") into catalog space
and bias every score along that direction. A slider at 50 is neutral.

Items you've watched or blocked are excluded outright, and with no
usable history (cold start) the engine falls back to a quality-floored
popularity ranking, so the row is never empty.

### Talk-back feedback

The `/recommendations` page shows everything the engine is currently
picking. React 👍/👎 or write a comment — "loved the slow-burn
tension", "too gory for me". Three things happen at once:

1. An LLM parses the comment into structured signal (sentiment,
   keyword boosts/mutes, genre exclusions, hard blocks).
2. The comment text is embedded into the *same vector space as the
   catalog* and joins your profile with the sentiment's sign — the
   words themselves steer future picks.
3. A background sync refreshes the Chillio rows within seconds.

With no LLM configured, a sentiment lexicon keeps 2–3 working.

### Measuring changes

`GET /admin/eval` runs a leave-last-N-out backtest: it hides each
user's most recent watches, rebuilds the profile from the rest, and
reports hit-rate + recall on the hidden items. Run it before and after
any ranking tweak.

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
picks into two managed Trakt lists (`Reclio • Recommended Movies`,
`Reclio • Recommended Shows`).

| Concern | Live call | Materialized |
| --- | --- | --- |
| `/feeds` latency budget (≤200ms) | ranking on every hit | zero extra work |
| Sync outage impact | every `/feeds` degrades | previous recs still serve |
| Chillio refreshes are frequent | recompute each time | cheap list read |
| Users see recs in Trakt too | no | yes — browsable at trakt.tv |

## Graceful degradation

If the engine has nothing to say (fresh install, embeddings disabled),
both rows fall back to a TMDB `discover` query filtered by the user's
top genres and preferences. Chillio always gets a valid 2-feed
response.

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
