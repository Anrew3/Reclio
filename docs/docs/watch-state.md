---
id: watch-state
title: Watch-state machine
sidebar_position: 7
---

# Watch-state machine

Most recommendation systems learn from one signal: what you watched. Reclio learns from two — what you watched, **and what you started but didn't finish**. The latter is just as informative, often more so. The watch-state machine is what extracts signal from those incomplete watches.

## The problem

Trakt's `/sync/playback` endpoint shows what's currently in-progress for each user — paused films, half-watched episodes. Without the watch-state machine, all of that data is invisible to Reclio's rec engine.

But raw "paused at 47%" doesn't tell you much on its own. Was that:

- *Fell asleep at 11:30 pm and forgot about it* (mild signal — they didn't dislike it)
- *Bailed at the lunch break* (strong signal — they actively didn't want to continue)
- *Got busy, will resume tomorrow* (no signal — wait and see)

The watch-state machine answers that question by combining **progress %**, **time elapsed**, and **the user's local hour at the moment they paused**.

## How it runs

Folded into the existing `sync_one_user` pipeline, no new cron job:

```
sync_one_user(user_id):
  build_taste_profile(...)
  _push_interactions(...)
  evaluate_watch_state(user_id, token)    ← here
  engine.get_recommendations(...)
  _refresh_managed_list(...)
```

It inherits the [adaptive cadence](./adaptive-sync) — hot users (1h), default (6h), cold (24h), dormant (weekly). Same per-user lock prevents overlapping evaluations.

Critical ordering: feedback signals (engine signal writes, taste cache invalidation) land *before* the next `get_recommendations` call. So a verdict reached this tick already affects this tick's recommendations.

## The decision tree

For movies, evaluated top to bottom:

| Condition | Verdict |
|---|---|
| Item appears in `/sync/history` since last sync | `completed` (positive signal) |
| `last_progress_pct >= 90` | `completed` (treating "skipped credits" as success) |
| `last_progress_pct < 5` AND age ≥ 24h | `accidental` (drop the row, no signal) |
| `local_hour ∈ [22, 04]` AND `5 ≤ pct ≤ 90` AND age ≥ **5 days** | `abandoned_sleep` |
| `local_hour ∉ [22, 04]` AND `5 ≤ pct ≤ 90` AND age ≥ **24h** | `abandoned_bounce` |
| Otherwise | stays `in_progress` (re-check next sync) |

For shows it's per-episode with show-level context:

| Condition | Verdict |
|---|---|
| Episode in `history` AND next episode in playback within 24h | `completed` (no signal — they're moving through) |
| **S1E1 + `pct < 50` + age ≥ 48h** | **`abandoned_bounce` on the SHOW** (loudest single signal) |
| S1E1 done but no E2 watched in 7 days | `abandoned_bounce` (mild — finished pilot but bounced) |
| Mid-season pause (E≥2 of S1, or any E of S≥2) | stays `in_progress` (normal — shows pause) |
| 2+ seasons watched AND no progress in 14 days | `abandoned_lost_interest` (positive on genre, neutral on show) |

The S1E1 bounce gets the strongest signal in the entire system: `AddRating(-1.0)` on the show. Rationale — they sat down to try a new show, didn't finish 20 minutes, walked away. That's the loudest "no" anyone can give.

## Engine signal feedback

| Verdict | Engine signal | Taste profile change |
|---|---|---|
| `completed` (movie) | `AddRating(+0.5)` | (already implicit via `/sync/history` push) |
| `accidental` | none | none |
| `abandoned_sleep` | `AddRating(-0.2)` | none — sleep ≠ dislike |
| `abandoned_bounce` (movie) | `AddRating(-0.7)` | -5% to top genres of that movie; mark `is_stale` |
| **`abandoned_bounce` (S1E1 of show)** | **`AddRating(-1.0)` on the SHOW** | -10% to show's top genres |
| `abandoned_lost_interest` | none on the show | light positive on the show's top genres |

Notice the `lost_interest` case is *positive* on the genre — the user invested seasons of their life in a show, they clearly love the genre. They just lost time/bandwidth for *this* show specifically.

## The sleep-vs-bounce heuristic

This is the trickiest part. The machine needs to know the user's local hour at the moment they paused — late-night pauses are most likely "fell asleep", daytime pauses are most likely "actively didn't want to continue".

We populate `users.timezone` from Trakt's `/users/me?extended=full` endpoint at OAuth and refresh on every sign-in. The IANA name (`America/Los_Angeles`, `Europe/London`, etc.) feeds Python's `zoneinfo` module to convert each `paused_at_utc` to the user's local hour at decision time.

The sleep window `[22, 23, 0, 1, 2, 3, 4]` is wide enough that DST jumps don't push a non-sleep event into the window or vice versa.

Edge case — shift workers: their "late night" is everyone else's morning. They'll get `abandoned_bounce` instead of `abandoned_sleep` for genuine sleep events. Mitigation for v1.6: learn each user's personal sleep pattern from their watch-history hour distribution. Out of scope for v1.5.

## Idempotency

Every `WatchAttempt` row carries a `feedback_pushed: bool` flag. The evaluator skips signal-pushing for any row where the flag is already `True`. So re-running the evaluator (which happens every sync tick) never double-counts.

## Inspecting verdicts

```bash
# All open + recently-decided attempts for a user, grouped by verdict
curl -H "X-Admin-Token: $TOKEN" \
     https://<your-host>/admin/watch_attempts/<user_id> | jq .
```

Returns:

```jsonc
{
  "user_id": "abc123",
  "total": 47,
  "counts": {
    "in_progress": 8,
    "completed": 31,
    "abandoned_sleep": 4,
    "abandoned_bounce": 2,
    "abandoned_lost_interest": 1,
    "accidental": 1
  },
  "by_status": {
    "abandoned_bounce": [
      {
        "kind": "episode",
        "show_tmdb_id": 1396,
        "season_number": 1,
        "episode_number": 1,
        "last_progress_pct": 32.4,
        "last_paused_at_utc": "2026-04-12T14:30:00",
        "last_paused_local_hour": 7,
        "decided_at": "2026-04-14T15:00:00",
        "feedback_pushed": true
      },
      ...
    ],
    ...
  }
}
```

## Hourly background sanity check

Independent of the watch-state machine, an [hourly health-check job](./api-reference#diagnostics-added-in-v15) probes every dependency (DB, Trakt, TMDB, engine, LLM) and logs a single WARNING with full diagnostic detail when anything degrades. Recovery shows up as INFO. Healthy installs produce zero WARNING-level lines.

State transitions:

| Transition | Log level |
|---|---|
| ok → ok | DEBUG only (never wakes anyone) |
| ok → failed | **WARNING** + full deep-dive in same line |
| ok → degraded | INFO with reason |
| failed → ok | INFO "RECOVERED" |
| failed → failed | DEBUG (no spam — admin endpoint still has it) |

Inspect via `/admin/health/history` (last 24 snapshots) or trigger immediately via `POST /admin/health/run`.
