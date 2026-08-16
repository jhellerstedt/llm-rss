# Weekly Feedback Stats Implementation Plan

> **For agentic workers:** Execute task-by-task. Steps use checkbox syntax.

**Goal:** Add per-category queued/posted/vote stats to the weekly `journal suggestions` Zulip digest.

**Architecture:** New `zulip_feedback_weekly_stats.py` period counters + `posted_events`; queue rows carry `bucket_id`; weekly summary merges config diff with stats and posts when either is non-empty.

**Tech Stack:** Python 3, existing Zulip helpers, unittest

## Global Constraints

- Counters are per `(realm, stream, bucket_id)`
- Dry-run writes nothing to the stats file
- Only successful **new** Zulip posts increment `posted` (not stale-head drops)
- Votes: `↑N / ↓M` from reacted bot posts only; join via `posted_events`

---

### Task 1: Stats module + unit tests

**Files:**
- Create: `zulip_feedback_weekly_stats.py`
- Create: `tests/test_zulip_feedback_weekly_stats.py`

**Produces:**
- `feedback_weekly_stats_path(config_path, zulip_cfg) -> Path`
- `resolve_bucket(group_name: str, feed_category: str | None) -> tuple[str, str, str]`  # id, title, kind
- `record_enqueued(...)`, `record_posted(...)`
- `load_stats` / `save_stats` / `reset_period_after_summary(...)`
- `aggregate_votes_for_stream(messages, posted_events, realm, stream) -> dict[bucket_id, tuple[up, down]]`
- `stats_nonzero_for_buckets(...)` / `format_stats_section(...)`

- [ ] Implement module + tests; run `python -m unittest tests.test_zulip_feedback_weekly_stats -v`

### Task 2: Queue instrumentation

**Files:**
- Modify: `zulip_feedback_queue.py`
- Modify: `tests/test_zulip_feedback_queue.py` (or create if missing)

- [ ] Persist `bucket_id`, `title`, `kind` on pending items
- [ ] `enqueue_feedback_ranking_for_group(..., bucket_id=, title=, kind=)`
- [ ] On append → `record_enqueued`; on successful send → `record_posted`
- [ ] Round-trip test for bucket fields

### Task 3: Weekly summary merge + post gate

**Files:**
- Modify: `zulip_journal_weekly_summary.py`
- Modify: `tests/test_zulip_journal_weekly_summary.py`

- [ ] Merge config + stats under shared headings
- [ ] Post when config diff OR stats non-zero per stream
- [ ] Reset stats period after successful digest cycle
- [ ] Tests for merge + gate

### Task 4: Wire main + direct post path

**Files:**
- Modify: `main.py`, `zulip_feedback.py` as needed

- [ ] Pass resolved bucket into enqueue / direct post
- [ ] Direct post records `posted` only
- [ ] Run full related unittest modules

---
