# Weekly feedback stats in journal suggestions — design

Status: approved (2026-08-11 brainstorm; build 2026-08-17)
Date: 2026-08-17

## Problem

The weekly Zulip digest under topic `journal suggestions` only reports
`config.toml` feed/keyword deltas. Operators also want a per-category
snapshot of feedback-ranking activity for the same period:

1. How many papers were **added to the queue**
2. How many were **posted** to Zulip
3. **Upvote / downvote** totals (`↑N / ↓M`) per category

Today enqueue/post leave no durable period counters (pending queue rows are
removed on dispatch), so those numbers cannot be reconstructed from the
queue file alone.

## Decisions (locked with user)

| Topic | Decision |
|-------|----------|
| Data source for queue counts | **Instrument** enqueue + successful new posts (exact counters going forward) |
| When to post | Weekly cadence posts if there is a **config diff and/or non-zero stats** for that stream |
| Vote display | Raw totals **`↑N / ↓M`**; skip posts with no thumb reactions |
| Granularity | **Per `feed_category`** (same category/group buckets as the config summary) |
| Approach | Period counters + `feed_category` / bucket on queue items + `posted_events` for vote join |

## Architecture overview

| Unit | File | Responsibility |
|------|------|----------------|
| Period stats | `zulip_feedback_weekly_stats.py` (new) | Load/save period JSON; bump enqueued/posted; record posted events; aggregate votes; format markdown; reset period |
| Queue | `zulip_feedback_queue.py` | Persist `bucket_id` (and title/kind) on pending rows; call stats hooks on enqueue append and successful send |
| Direct post | `zulip_feedback.py` / `main.py` | When queue is disabled, record **posted** (+ event) only — not enqueued |
| Weekly digest | `zulip_journal_weekly_summary.py` | Merge config diff + stats into one message; post if either is non-empty per stream; reset stats after a successful (non-dry-run) digest cycle |
| Category resolve | shared helpers | Same first-token `feed_category` / group-name buckets as `_bucket_id_title_for_group` |

### Data flow

```
enqueue (per actually appended pending row)
  └─ bump counters[realm|stream|bucket].enqueued += 1

dispatch send_message success (new post only; not "already in topic" drops)
  └─ bump counters[...].posted += 1
  └─ append posted_events {link, bucket_id, realm, stream, ts}

direct post path (feedback_ranking_use_queue=false, send success)
  └─ same posted bump + event (no enqueued)

weekly summary (interval elapsed)
  ├─ config_body = markdown_config_diff(...)
  ├─ stats_body  = format stats for allowed buckets on this stream
  │                 (votes: fetch feedback ranking msgs since period_start,
  │                  join link → bucket via posted_events for this realm/stream)
  ├─ if neither body: refresh config snap/timer; leave stats period unchanged
  ├─ else post header + merged sections
  └─ after all streams processed (non-dry-run): refresh config snap;
     reset stats counters + trim posted_events; set period_start=now
```

## State file

Path: `{config_stem}.feedback_weekly_stats.json` beside the config
(override optional later; default is enough).

```json
{
  "version": 1,
  "period_start_unix": 0,
  "counters": [
    {
      "realm": "tuesday",
      "stream": "science",
      "bucket_id": "c:cm",
      "title": "cm",
      "kind": "category",
      "enqueued": 12,
      "posted": 5
    }
  ],
  "posted_events": [
    {
      "link": "https://example.com/paper",
      "bucket_id": "c:cm",
      "realm": "tuesday",
      "stream": "science",
      "ts": 1710000000.0
    }
  ]
}
```

- Counters are **per (realm, stream, bucket)** so each stream digest shows
  activity for that stream’s queues, not inflated global totals.
- `posted_events` is a ring buffer (keep events newer than
  `period_start - 7d` or last ~2000 entries, whichever policy is simpler
  and safe). Used only to attribute Zulip reactions to buckets.
- Dry-run: no writes to this file.
- I/O failure: log and omit stats (config summary still runs).

## Buckets

Reuse weekly-summary bucket ids:

- With `feed_category` → `bucket_id = "c:{category}"`, `kind = "category"`, `title = category`
- Else → `bucket_id = "g:{group_name}"`, `kind = "group"`, `title = group_name`

Pass resolved bucket into enqueue from `main` / group context. Legacy
pending rows without bucket metadata count as `g:uncategorized` until
drained (or skip stats bump if missing — prefer `uncategorized` so counts
are not silently lost).

## Markdown

Merge into existing per-bucket sections when possible:

```text
### Category `cm`
- **Journal feeds:** …          # existing config lines, if any
- **Queued:** 12
- **Posted:** 5
- **Votes:** ↑8 / ↓3
```

If only stats (no config diff for that bucket), still emit the heading and
stats bullets. Header subtitle may note config and/or feedback activity.

**Non-zero stats** for the post gate: any allowed bucket on that stream with
`enqueued > 0` or `posted > 0` or `up + down > 0`.

## Votes

For each stream digest:

1. Fetch bot messages in topic `feedback ranking` since `period_start_unix`
   (reuse lookback/helpers where practical).
2. Parse paper link from body; normalize; look up matching `posted_events`
   for this realm/stream.
3. Sum `count_thumbs_reactions`; ignore messages with `up + down == 0`.
4. Ignore messages whose link is not in `posted_events` for this period/stream.

## Error handling

- Stats file corrupt → reset to empty period doc and log warning.
- Zulip fetch failure for votes → still show queued/posted; votes line
  omitted or `↑0 / ↓0` with log (prefer omit votes line on fetch failure).
- Never block config-diff posting solely because stats failed.

## Testing

Unit tests (no live Zulip):

- Counter bump / reset helpers
- Markdown merge (config-only, stats-only, both, stream filter)
- Vote aggregation from fake messages + `posted_events`
- Post gate: stats-only / config-only / neither
- Queue serialization round-trip preserves `bucket_id`

## Out of scope

- Retroactive stats for periods before deploy (counters start at zero)
- Changing adaptive feedback-control logic
- Embedding category text into Zulip feedback post bodies
