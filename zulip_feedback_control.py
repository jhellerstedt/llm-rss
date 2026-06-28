"""Adaptive control for Zulip feedback ranking: thresholds and enqueue rate."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zulip_feedback import (
    MAX_FEEDBACK_RANKING_POSTS_PER_GROUP,
    bot_identity_for_realm,
    count_thumbs_reactions,
    message_is_from_bot,
    parse_feedback_link_from_body,
    unique_realm_stream_pairs,
)
from zulip_feedback_queue import feedback_ranking_queue_path

logger = logging.getLogger(__name__)

CONTROL_STATE_VERSION = 1


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


def queue_depth_for_group(
    config_path: Path,
    zulip_cfg: dict[str, Any],
    zulip_sources: list[dict[str, Any]],
) -> int:
    path = feedback_ranking_queue_path(config_path, zulip_cfg)
    if not path.exists():
        return 0
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(doc, dict):
        return 0
    by_pair: dict[tuple[str, str], int] = {}
    for row in doc.get("queues") or []:
        if not isinstance(row, dict):
            continue
        realm = str(row.get("realm", "")).lower()
        stream = str(row.get("stream", ""))
        if not realm or not stream:
            continue
        pending = row.get("pending") or []
        by_pair[(realm, stream)] = len(pending) if isinstance(pending, list) else 0
    total = 0
    for realm, stream in unique_realm_stream_pairs(zulip_sources):
        total += by_pair.get((realm, stream), 0)
    return total


def compute_feedback_control(
    *,
    group_name: str,
    base_relevance: int,
    base_impact: int,
    period_hours: int,
    queue_depth: int,
    prior_margin: int,
    settings: FeedbackControlSettings,
    up_ratio: float,
    ratio_sample_count: int,
    consumption_posts_per_day: float,
) -> FeedbackControlResult:
    del group_name  # reserved for logging at call site
    if not settings.enabled:
        return FeedbackControlResult(
            threshold_margin=0,
            effective_relevance=base_relevance,
            effective_impact=base_impact,
            max_enqueue=settings.max_enqueue_per_run,
            up_ratio=up_ratio,
            ratio_sample_count=ratio_sample_count,
            consumption_posts_per_day=consumption_posts_per_day,
            queue_depth=queue_depth,
        )

    margin = max(0, min(settings.max_threshold_margin, prior_margin))
    if ratio_sample_count >= settings.ratio_min_samples:
        error = settings.target_up_ratio - up_ratio
        if error >= settings.ratio_deadband:
            margin = min(settings.max_threshold_margin, margin + settings.margin_step)
        elif error <= -settings.ratio_deadband:
            margin = max(0, margin - settings.margin_step)

    period = max(1, int(period_hours))
    feed_runs_per_day = 24 / period
    target_enqueue = (
        consumption_posts_per_day / feed_runs_per_day
        if feed_runs_per_day > 0
        else 0.0
    )
    if queue_depth > settings.target_queue_depth:
        target_enqueue *= 0.5

    max_enqueue = int(round(max(0.0, min(target_enqueue, settings.max_enqueue_per_run))))

    return FeedbackControlResult(
        threshold_margin=margin,
        effective_relevance=base_relevance + margin,
        effective_impact=base_impact + margin,
        max_enqueue=max_enqueue,
        up_ratio=up_ratio,
        ratio_sample_count=ratio_sample_count,
        consumption_posts_per_day=consumption_posts_per_day,
        queue_depth=queue_depth,
    )


def _default_control_doc() -> dict[str, Any]:
    return {"version": CONTROL_STATE_VERSION, "groups": {}}


def load_control_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _default_control_doc()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Feedback control JSON corrupt at %s; resetting", path)
        return _default_control_doc()
    if not isinstance(doc, dict):
        return _default_control_doc()
    if "groups" not in doc or not isinstance(doc.get("groups"), dict):
        doc = _default_control_doc()
    return doc


def save_control_state(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def apply_feedback_control_for_group(
    config_path: Path,
    cfg: dict[str, Any],
    *,
    group_name: str,
    base_relevance: int,
    base_impact: int,
    period_hours: int,
    zulip_sources: list[dict[str, Any]],
    messages_by_pair: dict[tuple[str, str], list[dict[str, Any]]],
    zulip_realms: dict[str, dict[str, str]],
    zulip_cfg: dict[str, Any],
) -> FeedbackControlResult:
    settings = load_feedback_control_settings(cfg)
    if not settings.enabled or not zulip_sources:
        return FeedbackControlResult(
            threshold_margin=0,
            effective_relevance=base_relevance,
            effective_impact=base_impact,
            max_enqueue=MAX_FEEDBACK_RANKING_POSTS_PER_GROUP,
            up_ratio=0.0,
            ratio_sample_count=0,
            consumption_posts_per_day=0.0,
            queue_depth=0,
        )

    posts = bot_feedback_posts_for_group(
        messages_by_pair, zulip_sources, zulip_realms
    )
    consumption = consumption_posts_per_day(
        posts, window_days=settings.consumption_window_days
    )
    up_ratio, ratio_n = up_ratio_from_recent_reacted(
        posts, sample_size=settings.ratio_sample_size
    )
    depth = queue_depth_for_group(config_path, zulip_cfg, zulip_sources)

    state_path = feedback_control_path(config_path, cfg)
    doc = load_control_state(state_path)
    groups = doc.setdefault("groups", {})
    prior_margin = int((groups.get(group_name) or {}).get("threshold_margin", 0))

    result = compute_feedback_control(
        group_name=group_name,
        base_relevance=base_relevance,
        base_impact=base_impact,
        period_hours=period_hours,
        queue_depth=depth,
        prior_margin=prior_margin,
        settings=settings,
        up_ratio=up_ratio,
        ratio_sample_count=ratio_n,
        consumption_posts_per_day=consumption,
    )

    groups[group_name] = {
        "threshold_margin": result.threshold_margin,
        "metrics": {
            "up_ratio": round(result.up_ratio, 4),
            "ratio_sample_count": result.ratio_sample_count,
            "consumption_posts_per_day": round(result.consumption_posts_per_day, 4),
            "queue_depth": result.queue_depth,
            "max_enqueue": result.max_enqueue,
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    doc["version"] = CONTROL_STATE_VERSION
    save_control_state(state_path, doc)

    logger.info(
        "[%s] feedback control: up_ratio=%.2f (n=%d), consume=%.2f/day, queue=%d, "
        "margin=%d → effective rel>%d imp>%d, enqueue≤%d",
        group_name,
        result.up_ratio,
        result.ratio_sample_count,
        result.consumption_posts_per_day,
        result.queue_depth,
        result.threshold_margin,
        result.effective_relevance,
        result.effective_impact,
        result.max_enqueue,
    )
    return result
