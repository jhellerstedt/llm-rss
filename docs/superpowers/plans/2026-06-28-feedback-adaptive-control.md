# Adaptive feedback control — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the loop between Zulip feedback reactions and RSS/feedback selectivity so enqueue rate tracks consumption and recent reactions trend toward 80% +1.

**Architecture:** New `zulip_feedback_control.py` module measures consumption (time window) and quality (last N reacted posts) from existing Zulip topic messages, persists per-group `threshold_margin` in a JSON sidecar, and returns effective thresholds + dynamic `max_enqueue` consumed by `process_group` and `_dispatch_group_feedback_posts`.

**Tech Stack:** Python 3, existing `zulip_feedback` / `zulip_feedback_queue` helpers, `unittest` (project convention).

**Spec:** [docs/superpowers/specs/2026-06-28-feedback-adaptive-control-design.md](../specs/2026-06-28-feedback-adaptive-control-design.md)

---

## File map

| File | Action | Responsibility |
|------|--------|----------------|
| `zulip_feedback_control.py` | Create | Config parsing, metrics, control logic, state I/O |
| `tests/test_zulip_feedback_control.py` | Create | Unit tests (TDD) |
| `main.py` | Modify | Wire control into `process_group` and feedback dispatch |
| `config.d/config.toml.example` | Modify | Document `[feedback_control]` table |
| `README.md` | Modify | Short section on adaptive control + queue balance |

---

### Task 1: Control config + state path helpers

**Files:**
- Create: `zulip_feedback_control.py`
- Create: `tests/test_zulip_feedback_control.py`

- [ ] **Step 1: Write failing tests for config defaults and state path**

```python
# tests/test_zulip_feedback_control.py
import tempfile
import unittest
from pathlib import Path

from zulip_feedback_control import (
    FeedbackControlSettings,
    feedback_control_path,
    load_feedback_control_settings,
)


class TestFeedbackControlConfig(unittest.TestCase):
    def test_default_settings(self) -> None:
        s = FeedbackControlSettings.from_cfg({})
        self.assertTrue(s.enabled)
        self.assertEqual(s.target_up_ratio, 0.80)
        self.assertEqual(s.consumption_window_days, 7)
        self.assertEqual(s.ratio_sample_size, 20)
        self.assertEqual(s.ratio_min_samples, 5)
        self.assertEqual(s.max_threshold_margin, 3)
        self.assertEqual(s.max_enqueue_per_run, 2)
        self.assertEqual(s.target_queue_depth, 2)
        self.assertEqual(s.margin_step, 1)
        self.assertEqual(s.ratio_deadband, 0.10)

    def test_disabled_via_cfg(self) -> None:
        s = FeedbackControlSettings.from_cfg({"feedback_control": {"enabled": False}})
        self.assertFalse(s.enabled)

    def test_state_path_default(self) -> None:
        p = Path("/tmp/x/config.toml")
        self.assertEqual(
            feedback_control_path(p, {}),
            Path("/tmp/x/config.feedback_control.json"),
        )

    def test_state_path_override(self) -> None:
        p = Path("/tmp/x/config.toml")
        out = feedback_control_path(p, {"feedback_control": {"file": "state/fc.json"}})
        self.assertEqual(out, Path("/tmp/x/state/fc.json").resolve())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_zulip_feedback_control.TestFeedbackControlConfig -v`  
Expected: FAIL (`ModuleNotFoundError` or missing attributes)

- [ ] **Step 3: Implement minimal config + path module**

```python
# zulip_feedback_control.py (initial skeleton)
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FeedbackControlSettings:
    enabled: bool = True
    target_up_ratio: float = 0.80
    consumption_window_days: int = 7
    ratio_sample_size: int = 20
    ratio_min_samples: int = 5
    max_threshold_margin: int = 3
    max_enqueue_per_run: int = 2
    target_queue_depth: int = 2
    margin_step: int = 1
    ratio_deadband: float = 0.10
    file: str | None = None

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> FeedbackControlSettings:
        raw = dict(cfg.get("feedback_control") or {})
        return cls(
            enabled=bool(raw.get("enabled", True)),
            target_up_ratio=float(raw.get("target_up_ratio", 0.80)),
            consumption_window_days=int(raw.get("consumption_window_days", 7)),
            ratio_sample_size=int(raw.get("ratio_sample_size", 20)),
            ratio_min_samples=int(raw.get("ratio_min_samples", 5)),
            max_threshold_margin=int(raw.get("max_threshold_margin", 3)),
            max_enqueue_per_run=int(raw.get("max_enqueue_per_run", 2)),
            target_queue_depth=int(raw.get("target_queue_depth", 2)),
            margin_step=int(raw.get("margin_step", 1)),
            ratio_deadband=float(raw.get("ratio_deadband", 0.10)),
            file=str(raw["file"]) if raw.get("file") else None,
        )


def load_feedback_control_settings(cfg: dict[str, Any]) -> FeedbackControlSettings:
    return FeedbackControlSettings.from_cfg(cfg)


def feedback_control_path(config_path: Path, cfg: dict[str, Any]) -> Path:
    settings = load_feedback_control_settings(cfg)
    if settings.file:
        p = Path(settings.file)
        if not p.is_absolute():
            return (config_path.parent / p).resolve()
        return p.resolve()
    return config_path.with_name(f"{config_path.stem}.feedback_control.json")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_zulip_feedback_control.TestFeedbackControlConfig -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add zulip_feedback_control.py tests/test_zulip_feedback_control.py
git commit -m "Add feedback control config and state path helpers."
```

---

### Task 2: Metric extraction from Zulip messages

**Files:**
- Modify: `zulip_feedback_control.py`
- Modify: `tests/test_zulip_feedback_control.py`

- [ ] **Step 1: Write failing tests for consumption and ratio metrics**

```python
import time
from zulip_feedback_control import (
    bot_feedback_posts_for_group,
    consumption_posts_per_day,
    up_ratio_from_recent_reacted,
)


class TestFeedbackControlMetrics(unittest.TestCase):
    def _bot_post(self, url: str, ts: int, reactions: list | None = None) -> dict:
        return {
            "content": f"Title\n\nLink: {url}",
            "timestamp": ts,
            "reactions": reactions or [],
            "sender_email": "bot@example.com",
        }

    def test_consumption_counts_posts_in_window(self) -> None:
        now = int(time.time())
        old = now - 10 * 86400
        posts = [self._bot_post("https://a.org/1", now), self._bot_post("https://a.org/2", old)]
        rate = consumption_posts_per_day(posts, window_days=7, now_ts=now)
        self.assertEqual(rate, 1 / 7)

    def test_up_ratio_last_n_reacted(self) -> None:
        posts = [
            self._bot_post("https://a.org/1", 1, [{"emoji_name": "+1", "user_id": 1}]),
            self._bot_post("https://a.org/2", 2, [{"emoji_name": "-1", "user_id": 2}]),
            self._bot_post("https://a.org/3", 3, [{"emoji_name": "+1", "user_id": 3}]),
            self._bot_post("https://a.org/4", 4, []),  # no reaction → excluded from ratio sample
        ]
        ratio, n = up_ratio_from_recent_reacted(posts, sample_size=2)
        self.assertEqual(n, 2)
        self.assertEqual(ratio, 0.5)  # last 2 reacted: +1, -1

    def test_bot_feedback_posts_filters_non_bot_and_no_link(self) -> None:
        msgs = [
            self._bot_post("https://a.org/1", 100),
            {"content": "human chat", "timestamp": 101, "sender_email": "human@x.com"},
            {"content": "no link", "timestamp": 102, "sender_email": "bot@example.com"},
        ]
        by_pair = {("r1", "s1"): msgs}
        zulip_sources = [{"realm": "R1", "stream": "s1"}]
        zulip_realms = {"r1": {"email": "bot@example.com"}}
        out = bot_feedback_posts_for_group(by_pair, zulip_sources, zulip_realms)
        self.assertEqual(len(out), 1)
        self.assertIn("a.org/1", out[0]["content"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_zulip_feedback_control.TestFeedbackControlMetrics -v`  
Expected: FAIL

- [ ] **Step 3: Implement metric helpers**

Add to `zulip_feedback_control.py`:

```python
import time
from typing import Any

from zulip_feedback import (
    bot_identity_for_realm,
    count_thumbs_reactions,
    message_is_from_bot,
    parse_feedback_link_from_body,
    unique_realm_stream_pairs,
)


def _normalize_ts(ts: int | float) -> int:
    t = int(ts)
    return t // 1000 if t > 10_000_000_000 else t


def bot_feedback_posts_for_group(
    messages_by_pair: dict[tuple[str, str], list[dict[str, Any]]],
    zulip_sources: list[dict[str, Any]],
    zulip_realms: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    for realm, stream in unique_realm_stream_pairs(zulip_sources):
        bot_email, bot_name = bot_identity_for_realm(zulip_realms, realm)
        for msg in messages_by_pair.get((realm, stream), []):
            if not message_is_from_bot(msg, bot_email, bot_name):
                continue
            if not parse_feedback_link_from_body(str(msg.get("content") or "")):
                continue
            posts.append(msg)
    posts.sort(key=lambda m: _normalize_ts(m.get("timestamp") or 0))
    return posts


def consumption_posts_per_day(
    posts: list[dict[str, Any]],
    *,
    window_days: int,
    now_ts: int | None = None,
) -> float:
    if window_days <= 0:
        return 0.0
    now = _normalize_ts(now_ts if now_ts is not None else time.time())
    cutoff = now - window_days * 86400
    count = sum(
        1 for p in posts if _normalize_ts(p.get("timestamp") or 0) >= cutoff
    )
    return count / window_days


def up_ratio_from_recent_reacted(
    posts: list[dict[str, Any]],
    *,
    sample_size: int,
) -> tuple[float, int]:
    reacted: list[dict[str, Any]] = []
    for msg in reversed(posts):
        up, down = count_thumbs_reactions(msg)
        if up + down <= 0:
            continue
        reacted.append(msg)
        if len(reacted) >= sample_size:
            break
    if not reacted:
        return 0.0, 0
    total_up = total = 0
    for msg in reacted:
        up, down = count_thumbs_reactions(msg)
        total_up += up
        total += up + down
    return (total_up / total if total else 0.0), len(reacted)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_zulip_feedback_control.TestFeedbackControlMetrics -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add zulip_feedback_control.py tests/test_zulip_feedback_control.py
git commit -m "Add Zulip feedback consumption and ratio metrics."
```

---

### Task 3: Queue depth + control logic

**Files:**
- Modify: `zulip_feedback_control.py`
- Modify: `tests/test_zulip_feedback_control.py`

- [ ] **Step 1: Write failing tests for margin adjustment and enqueue sizing**

```python
from zulip_feedback_control import (
    FeedbackControlResult,
    compute_feedback_control,
    queue_depth_for_group,
)


class TestFeedbackControlLogic(unittest.TestCase):
    def test_margin_increases_when_up_ratio_low(self) -> None:
        settings = FeedbackControlSettings(
            ratio_min_samples=3,
            ratio_deadband=0.05,
            margin_step=1,
            max_threshold_margin=3,
        )
        result = compute_feedback_control(
            group_name="g1",
            base_relevance=5,
            base_impact=3,
            period_hours=24,
            posts=[],  # unused when metrics passed
            queue_depth=0,
            prior_margin=0,
            settings=settings,
            up_ratio=0.60,
            ratio_sample_count=10,
            consumption_posts_per_day=1.0,
        )
        self.assertEqual(result.threshold_margin, 1)
        self.assertEqual(result.effective_relevance, 6)
        self.assertEqual(result.effective_impact, 4)

    def test_margin_decreases_when_up_ratio_high(self) -> None:
        settings = FeedbackControlSettings(ratio_min_samples=3, ratio_deadband=0.05)
        result = compute_feedback_control(
            group_name="g1",
            base_relevance=5,
            base_impact=3,
            period_hours=24,
            posts=[],
            queue_depth=0,
            prior_margin=2,
            settings=settings,
            up_ratio=0.95,
            ratio_sample_count=10,
            consumption_posts_per_day=1.0,
        )
        self.assertEqual(result.threshold_margin, 1)

    def test_enqueue_matches_consumption_one_run_per_day(self) -> None:
        settings = FeedbackControlSettings(max_enqueue_per_run=2, target_queue_depth=2)
        result = compute_feedback_control(
            group_name="g1",
            base_relevance=5,
            base_impact=3,
            period_hours=24,
            posts=[],
            queue_depth=0,
            prior_margin=0,
            settings=settings,
            up_ratio=0.80,
            ratio_sample_count=0,
            consumption_posts_per_day=1.0,
        )
        self.assertEqual(result.max_enqueue, 1)

    def test_enqueue_halved_when_queue_deep(self) -> None:
        settings = FeedbackControlSettings(max_enqueue_per_run=2, target_queue_depth=2)
        result = compute_feedback_control(
            group_name="g1",
            base_relevance=5,
            base_impact=3,
            period_hours=24,
            posts=[],
            queue_depth=5,
            prior_margin=0,
            settings=settings,
            up_ratio=0.80,
            ratio_sample_count=0,
            consumption_posts_per_day=2.0,
        )
        self.assertEqual(result.max_enqueue, 1)  # raw 2, halved → 1

    def test_disabled_returns_baseline(self) -> None:
        settings = FeedbackControlSettings(enabled=False)
        result = compute_feedback_control(
            group_name="g1",
            base_relevance=5,
            base_impact=3,
            period_hours=24,
            posts=[],
            queue_depth=99,
            prior_margin=3,
            settings=settings,
            up_ratio=0.0,
            ratio_sample_count=99,
            consumption_posts_per_day=99.0,
        )
        self.assertEqual(result.threshold_margin, 0)
        self.assertEqual(result.effective_relevance, 5)
        self.assertEqual(result.max_enqueue, 2)
```

Add queue depth test with a temp queue JSON file in a separate test method (write minimal queue doc, call `queue_depth_for_group`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_zulip_feedback_control.TestFeedbackControlLogic -v`  
Expected: FAIL

- [ ] **Step 3: Implement control logic + queue depth**

Add `FeedbackControlResult` dataclass and:

```python
@dataclass(frozen=True)
class FeedbackControlResult:
    threshold_margin: int
    effective_relevance: int
    effective_impact: int
    max_enqueue: int
    up_ratio: float
    ratio_sample_count: int
    consumption_posts_per_day: float
    queue_depth: int


def queue_depth_for_group(
    config_path: Path,
    zulip_cfg: dict[str, Any],
    zulip_sources: list[dict[str, Any]],
) -> int:
    from zulip_feedback_queue import _doc_to_by_pair, feedback_ranking_queue_path

    path = feedback_ranking_queue_path(config_path, zulip_cfg)
    if not path.exists():
        return 0
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    by_pair = _doc_to_by_pair(doc if isinstance(doc, dict) else {})
    total = 0
    for realm, stream in unique_realm_stream_pairs(zulip_sources):
        total += len(by_pair.get((realm, stream)) or [])
    return total


def compute_feedback_control(...) -> FeedbackControlResult:
    # if not settings.enabled: return baseline + max_enqueue_per_run
    # adjust margin from up_ratio vs target with deadband + clamp
    # target_enqueue = consumption / (24/period); halve if queue_depth > target_queue_depth
    # max_enqueue = round clamp to [0, max_enqueue_per_run]
    # effective = base + margin
```

Prefer importing `_doc_to_by_pair` only if needed; alternatively duplicate minimal queue read to avoid private import — use public `feedback_ranking_queue_path` + inline parse matching tests.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_zulip_feedback_control -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add zulip_feedback_control.py tests/test_zulip_feedback_control.py
git commit -m "Implement adaptive margin and enqueue control logic."
```

---

### Task 4: State persistence + orchestration API

**Files:**
- Modify: `zulip_feedback_control.py`
- Modify: `tests/test_zulip_feedback_control.py`

- [ ] **Step 1: Write failing test for end-to-end `apply_feedback_control_for_group`**

```python
from zulip_feedback_control import apply_feedback_control_for_group


class TestFeedbackControlState(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = Path(self.tmp.name) / "cfg.toml"
        self.cfg_path.write_text("# stub\n", encoding="utf-8")

    def test_persists_margin_after_apply(self) -> None:
        now = int(time.time())
        msgs = [{
            "content": "P\n\nLink: https://a.org/1",
            "timestamp": now,
            "reactions": [{"emoji_name": "-1", "user_id": 1}],
            "sender_email": "bot@example.com",
        }]
        by_pair = {("r1", "s1"): msgs}
        cfg = {
            "feedback_control": {"ratio_min_samples": 1, "ratio_deadband": 0.0},
            "groups": [{"name": "g1", "relevance_threshold": 5, "impact_threshold": 3,
                        "period": 24, "zulip_sources": [{"realm": "R1", "stream": "s1"}]}],
        }
        result = apply_feedback_control_for_group(
            self.cfg_path,
            cfg,
            group_name="g1",
            base_relevance=5,
            base_impact=3,
            period_hours=24,
            zulip_sources=[{"realm": "R1", "stream": "s1"}],
            messages_by_pair=by_pair,
            zulip_realms={"r1": {"email": "bot@example.com"}},
            zulip_cfg={},
        )
        self.assertEqual(result.threshold_margin, 1)
        state_path = feedback_control_path(self.cfg_path, cfg)
        self.assertTrue(state_path.exists())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_zulip_feedback_control.TestFeedbackControlState -v`  
Expected: FAIL

- [ ] **Step 3: Implement `load/save` state + `apply_feedback_control_for_group`**

```python
def load_control_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "groups": {}}
    ...

def save_control_state(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

def apply_feedback_control_for_group(...) -> FeedbackControlResult:
    settings = load_feedback_control_settings(cfg)
    if not settings.enabled or not zulip_sources:
        return FeedbackControlResult(...)  # baseline
    posts = bot_feedback_posts_for_group(...)
    consumption = consumption_posts_per_day(posts, window_days=settings.consumption_window_days)
    up_ratio, ratio_n = up_ratio_from_recent_reacted(posts, sample_size=settings.ratio_sample_size)
    depth = queue_depth_for_group(config_path, zulip_cfg, zulip_sources)
    prior_margin = int(state["groups"].get(group_name, {}).get("threshold_margin", 0))
    result = compute_feedback_control(..., prior_margin=prior_margin, ...)
    # merge into state doc, save, log
    return result
```

Use file locking similar to queue if concurrent runs are possible; for v1, simple read/write is acceptable (match author_whitelist pattern).

- [ ] **Step 4: Run full test module**

Run: `python -m unittest tests.test_zulip_feedback_control -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add zulip_feedback_control.py tests/test_zulip_feedback_control.py
git commit -m "Persist per-group feedback control state across runs."
```

---

### Task 5: Wire into `main.py`

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Import and call control after loading feedback state**

In `process_group`, after `load_feedback_state_for_group` (~line 452):

```python
from zulip_feedback_control import apply_feedback_control_for_group

feedback_control = None
if zulip_sources and zulip_realms:
    feedback_control = apply_feedback_control_for_group(
        config_path,
        cfg,  # full config dict — pass from process_group arg
        group_name=group_name,
        base_relevance=relevance_threshold,
        base_impact=impact_threshold,
        period_hours=int(group["period"]),
        zulip_sources=zulip_sources,
        messages_by_pair=feedback_msgs_by_pair,
        zulip_realms=zulip_realms,
        zulip_cfg=zulip_cfg,
    )
    relevance_threshold = feedback_control.effective_relevance
    impact_threshold = feedback_control.effective_impact
```

Add `cfg: dict` parameter to `process_group` if not already available (load in `main()` and pass through).

- [ ] **Step 2: Pass dynamic max_enqueue to feedback dispatch**

Extend `_dispatch_group_feedback_posts`:

```python
def _dispatch_group_feedback_posts(..., max_posts: int | None = None) -> None:
    feedback_post_links = select_top_ranked_for_feedback_posts(
        batch.title_link_scores,
        max_posts=max_posts if max_posts is not None else MAX_FEEDBACK_RANKING_POSTS_PER_GROUP,
        single_author_impact_penalty=single_author_impact_penalty,
    )
```

Store `feedback_control.max_enqueue` on `GroupRunResult` or pass from `process_group` return through to dispatch loop in `main()`.

Simplest: add optional field on `GroupFeedbackCandidates`:

```python
@dataclass
class GroupFeedbackCandidates:
    ...
    max_posts: int = MAX_FEEDBACK_RANKING_POSTS_PER_GROUP
```

Set when building `feedback_batch` in `process_group`.

- [ ] **Step 3: Log line per spec**

Inside `apply_feedback_control_for_group` or after call in `process_group`:

```python
logger.info(
    "[%s] feedback control: up_ratio=%.2f (n=%d), consume=%.2f/day, queue=%d, "
    "margin=%d → effective rel>%d imp>%d, enqueue≤%d",
    group_name,
    feedback_control.up_ratio,
    feedback_control.ratio_sample_count,
    feedback_control.consumption_posts_per_day,
    feedback_control.queue_depth,
    feedback_control.threshold_margin,
    feedback_control.effective_relevance,
    feedback_control.effective_impact,
    feedback_control.max_enqueue,
)
```

- [ ] **Step 4: Run existing tests + new tests**

Run: `python -m unittest discover -s tests -v`  
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "Wire adaptive feedback control into group processing."
```

---

### Task 6: Documentation

**Files:**
- Modify: `config.d/config.toml.example`
- Modify: `README.md`

- [ ] **Step 1: Add `[feedback_control]` block to example config** (see spec defaults + comment that it requires `zulip_sources`)

- [ ] **Step 2: Add README subsection** under Zulip feedback / queue explaining:
  - adaptive thresholds track 80/20 reactions
  - enqueue rate tracks consumption
  - state file location
  - works with `feedback_ranking_use_queue`

- [ ] **Step 3: Commit**

```bash
git add config.d/config.toml.example README.md
git commit -m "Document adaptive feedback control configuration."
```

---

## Spec coverage checklist

| Spec requirement | Task |
|------------------|------|
| Per-group control | Task 4–5 |
| Hybrid measurement (time + last N) | Task 2–3 |
| Single threshold margin on both thresholds | Task 3 |
| Enqueue ≈ consumption + queue balance | Task 3 |
| 80/20 target with deadband | Task 3 |
| JSON state sidecar | Task 4 |
| Whitelist bypass unchanged | No code change (verify in Task 5) |
| Disabled / no zulip_sources fallback | Task 3–4 |
| Logging | Task 5 |
| Config docs | Task 6 |

## Verification (manual)

After implementation, with a test config using `feedback_ranking_use_queue = true`:

1. Run feed: `python main.py --config-path config.d/config.toml --dryrun` — confirm log shows control metrics.
2. Inspect `{stem}.feedback_control.json` for updated margin/metrics.
3. Run dispatch dryrun: `python main.py --dispatch-feedback-queue --dryrun` — unchanged behavior.

---

**Plan complete and saved to `docs/superpowers/plans/2026-06-28-feedback-adaptive-control.md`.**

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks  
2. **Inline Execution** — implement tasks in this session with checkpoints

Which approach do you want?
