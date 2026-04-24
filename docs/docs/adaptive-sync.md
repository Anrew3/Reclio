---
id: adaptive-sync
title: Adaptive sync
sidebar_position: 7
---

# Adaptive sync

Each user's taste profile re-syncs on a cadence that adapts to how
often they actually open Chillio. No point re-computing recs every 4
hours for someone who opens the app once a week — and 4 hours is too
slow for a daily user.

## The model

Reclio keeps a rolling list of `/feeds` request timestamps on each
user row (`recent_feed_hits` JSON, trimmed to the last 50). On every
scheduler sweep it counts hits in the last 7 days and picks a bucket:

| Hits in last 7d | Bucket | Default interval |
| --- | --- | --- |
| ≥ `USER_SYNC_HOT_THRESHOLD_PER_WEEK` (14) | **hot** | 4h |
| ≤ `USER_SYNC_COLD_THRESHOLD_PER_WEEK` (3) | **cold** | 24h |
| Everything between | **default** | 8h |

A user is re-synced only if their `TasteCache.computed_at` is older
than the bucket interval. Otherwise the sweep skips them.

## How the sweep runs

The scheduler fires every `USER_SYNC_SWEEP_INTERVAL_HOURS` (default
1h). Inside the sweep:

1. Fetch all users with Trakt tokens.
2. For each user, compute `interval_hours = adaptive_sync_interval_hours(user)`.
3. If `TasteCache.computed_at < now − interval_hours`, queue a full
   `sync_one_user(...)`.
4. Aggregate bucket counts (`hot`, `default`, `cold`) for the log
   line.

A per-user `asyncio.Lock` serializes concurrent syncs for the same
user so two schedulers (or a scheduler + an admin POST) can't step on
each other.

## Overriding the defaults

All thresholds are env-driven:

```env
USER_SYNC_DEFAULT_INTERVAL_HOURS=8
USER_SYNC_HOT_INTERVAL_HOURS=4
USER_SYNC_COLD_INTERVAL_HOURS=24
USER_SYNC_HOT_THRESHOLD_PER_WEEK=14
USER_SYNC_COLD_THRESHOLD_PER_WEEK=3
USER_SYNC_SWEEP_INTERVAL_HOURS=1
```

If you want everyone on a fixed schedule, set the three interval
values to the same number. The thresholds then don't matter.

## Why not a separate activity-log table?

Considered and rejected. A bounded JSON column on the `users` row:

- Lives in the row Reclio already hydrates on every `/feeds` hit, so
  there's no extra read.
- Is trivial to trim (last 50 hits).
- Works under SQLite without a migration beyond adding the column.
- Stays at `O(hundreds of bytes)` per user.

A proper table would be warranted once we want per-device or
per-endpoint analytics. That's [future work](../intro#where-to-go-next).
