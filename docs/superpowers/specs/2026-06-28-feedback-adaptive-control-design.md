# Adaptive feedback control — design

Status: approved (2026-06-28)
Date: 2026-06-28

## Problem

The Zulip "feedback ranking" pipeline today uses **fixed** RSS thresholds and enqueues
**up to two** papers per group per feed run, while dispatch posts **one** item per stream
per cron run (after a reaction). There is no feedback loop:

- The queue can grow if enqueue outpaces consumption, or sit empty if the opposite.
- There is no target for how often the team reacts `:+1:` vs `:-1:` on posted papers.

We want a closed loop so that:

1. **Throughput** — items added to the feedback queue (or posted directly) match the rate
   at which the queue is consumed (posted to Zulip).
2. **Quality** — recent Zulip reactions trend toward **80% +1 / 20% −1**, by raising or
   lowering how selective the pipeline is.
3. **Scope** — both RSS inclusion **and** feedback-ranking picks use the same adaptive
   bar (option B from brainstorming).

## Decisions (locked with user)

| Topic | Decision |
|-------|----------|
| What adjusts | RSS `relevance_threshold` / `impact_threshold` **and** feedback enqueue count |
| Granularity | **Per group** — each `[[groups]]` entry has its own controller state |
| Measurement | **Hybrid (C):** rolling **time window** for consumption rate; **last N reacted posts** for +1/−1 ratio |
| Control knob | **Single threshold margin** added equally to both relevance and impact baselines |
| Whitelist | Unchanged — whitelisted authors still bypass thresholds entirely |

## Architecture overview

One new module plus small edits to `main.py` and config docs:

| Unit | File | Responsibility |
|------|------|----------------|
| Controller | `zulip_feedback_control.py` | Measure metrics from Zulip topic messages + queue depth; compute `threshold_margin` and `max_enqueue`; load/save JSON state |
| Pipeline hook | `main.py` | Before threshold filter in `process_group`, apply effective thresholds; pass dynamic `max_posts` into feedback dispatch |
| Queue (existing) | `zulip_feedback_queue.py` | Unchanged dispatch; queue depth read for throughput loop |
| Feedback (existing) | `zulip_feedback.py` | Unchanged reaction parsing; metrics reuse `aggregate_feedback_signals`, bot post detection, timestamps |

### Data flow

```
feed run (process_group, per group with zulip_sources)
  ├─ load Zulip feedback topic messages (existing)
  ├─ read queue depth for group's (realm, stream) pairs
  ├─ compute metrics:
  │     consumption_per_day  ← bot posts in window / window_days
  │     up_ratio             ← last N reacted bot posts across group's streams
  │     queue_depth          ← sum of pending items on those streams
  ├─ update threshold_margin + max_enqueue in feedback_control.json
  ├─ effective_rel = config.relevance_threshold + margin
  │   effective_imp = config.impact_threshold + margin
  ├─ passing filter uses effective thresholds (whitelist bypass unchanged)
  ├─ select_top_ranked_for_feedback_posts(..., max_posts=max_enqueue)
  └─ enqueue or post (existing paths)

dispatch run (--dispatch-feedback-queue)
  └─ unchanged (1 post/stream when latest bot post has a reaction)
```

Consumption is derived from **Zulip bot post timestamps** in the feedback topic, not
from queue-file writes alone, so the controller stays accurate if cron cadence changes.

## Control loops

Both loops run once per feed run, per group, before scoring results are filtered.

### Quality loop (80/20 target)

**Input:** last `ratio_sample_size` bot posts (default 20) that have at least one
`:+1:` or `:-1:` reaction, aggregated across all `(realm, stream)` pairs in the
group's `zulip_sources`. Posts without reactions are skipped when building the sample.

**Metric:**

```
up_ratio = sum(+1) / sum(+1 + -1)
error = target_up_ratio - up_ratio    # default target 0.80
```

**Adjustment:** only when `ratio_sample_count >= ratio_min_samples` (default 5).

```
if error >= ratio_deadband:   threshold_margin += margin_step   # too many -1s → stricter
elif error <= -ratio_deadband: threshold_margin -= margin_step  # too many +1s → looser
clamp threshold_margin to [0, max_threshold_margin]
```

Default `ratio_deadband = 0.10` (no change within ±10% of target) to reduce jitter.
Default `margin_step = 1`.

**Effective thresholds:**

```
effective_relevance  = group.relevance_threshold  + threshold_margin
effective_impact     = group.impact_threshold     + threshold_margin
```

Config TOML values remain the neutral setpoint; the JSON sidecar holds the offset.

### Throughput loop (enqueue ≈ consume)

**Input:** count of bot feedback-ranking posts in the last `consumption_window_days`
(default 7), aggregated across the group's streams.

```
consumption_per_day = post_count / consumption_window_days
feed_runs_per_day   = 24 / group.period    # period in hours
target_enqueue      = consumption_per_day / feed_runs_per_day
```

**Queue balance** (sum of pending items on the group's streams in the queue file):

```
if queue_depth > target_queue_depth:
    target_enqueue *= 0.5          # back off when backed up
if queue_depth == 0 and consumption_per_day > 0:
    target_enqueue unchanged       # allow full rate when empty

max_enqueue = round(clamp(target_enqueue, 0, max_enqueue_per_run))
```

When `[zulip] feedback_ranking_use_queue = false`, the same `max_enqueue` caps direct
posts via `post_feedback_ranking_for_new_items`.

## State file

Path: `{config_stem}.feedback_control.json` beside the TOML (override via config).

```json
{
  "version": 1,
  "groups": {
    "cm_physics": {
      "threshold_margin": 1,
      "metrics": {
        "up_ratio": 0.75,
        "ratio_sample_count": 18,
        "consumption_posts_per_day": 0.43,
        "queue_depth": 2,
        "max_enqueue": 1
      },
      "updated_at": "2026-06-28T12:00:00Z"
    }
  }
}
```

Follow the project's file-based state pattern (like the feedback queue JSON and author
whitelist). Gitignore local state in deployments if desired.

## Configuration

New optional table in TOML (defaults shown):

```toml
[feedback_control]
enabled = true
target_up_ratio = 0.80
consumption_window_days = 7
ratio_sample_size = 20
ratio_min_samples = 5
max_threshold_margin = 3
max_enqueue_per_run = 2
target_queue_depth = 2
margin_step = 1
ratio_deadband = 0.10
# Optional path (relative to TOML directory):
# file = "data/feedback_control.json"
```

When `enabled = false` or a group has no `zulip_sources`, behavior reverts to today's
fixed thresholds and `MAX_FEEDBACK_RANKING_POSTS_PER_GROUP` (2).

## Logging

Per group, when control is active:

```
[cm_physics] feedback control: up_ratio=0.75 (n=18), consume=0.43/day, queue=2,
  margin=1 → effective rel>6 imp>4, enqueue≤1
```

## Edge cases

| Case | Behavior |
|------|----------|
| Cold start (no reacted posts) | `threshold_margin = 0`; throughput loop still sets enqueue from post history if any |
| Fewer than `ratio_min_samples` reacted posts | Do not change margin; log sample count |
| Multiple streams per group | Aggregate consumption, ratio sample, and queue depth across all pairs |
| dryrun | Compute and log; persist state (metrics only) so operators can inspect — same as queue dryrun logging |
| Cross-group dedup | Unchanged; adaptive thresholds apply before dedup within each group |
| Whitelist | Still bypasses effective thresholds |

## Testing

Unit tests in `tests/test_zulip_feedback_control.py`:

- Metric extraction from synthetic message lists (timestamps, reactions)
- Margin adjusts up when `up_ratio < target`, down when above (respect deadband)
- Enqueue scales with consumption and backs off when queue deep
- Effective thresholds = config + margin
- Disabled / no zulip_sources → no-op (returns config thresholds and default max enqueue)
- State file round-trip

## Out of scope (v1)

- Separate relevance vs impact margins (dual controller)
- Per-stream thresholds within one group
- Changing LLM prompt weight for prior reactions (only threshold + enqueue control)
- Automatic adjustment of dispatch cron frequency (still operator-scheduled)
