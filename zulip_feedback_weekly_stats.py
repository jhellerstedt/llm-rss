"""Period counters for weekly feedback-ranking stats in journal suggestions digests."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from rss_merge import normalize_link
from zulip_feedback import (
    count_thumbs_reactions,
    parse_feedback_link_from_body,
    _normalize_message_ts,
)

logger = logging.getLogger(__name__)

STATS_VERSION = 1
MAX_POSTED_EVENTS = 2000
EVENT_RETENTION_PAD_SEC = 7 * 24 * 3600


def feedback_weekly_stats_path(config_path: Path, zulip_cfg: dict[str, Any] | None = None) -> Path:
    zulip_cfg = zulip_cfg or {}
    rel = zulip_cfg.get("feedback_weekly_stats_file")
    if rel:
        p = Path(str(rel))
        if not p.is_absolute():
            return (config_path.parent / p).resolve()
        return p.resolve()
    return config_path.with_name(f"{config_path.stem}.feedback_weekly_stats.json")


def resolve_bucket(group_name: str, feed_category: str | None) -> tuple[str, str, str]:
    """Return (bucket_id, title, kind) matching journal weekly summary buckets."""
    fc = str(feed_category or "").strip()
    if fc:
        token = fc.split()[0]
        return f"c:{token}", token, "category"
    name = str(group_name or "unnamed").strip() or "unnamed"
    return f"g:{name}", name, "group"


def _default_doc(*, period_start: float | None = None) -> dict[str, Any]:
    return {
        "version": STATS_VERSION,
        "period_start_unix": float(period_start if period_start is not None else time.time()),
        "counters": [],
        "posted_events": [],
    }


def load_stats(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _default_doc()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("Feedback weekly stats not a dict at %s; resetting", path)
            return _default_doc()
        data.setdefault("version", STATS_VERSION)
        data.setdefault("period_start_unix", time.time())
        if not isinstance(data.get("counters"), list):
            data["counters"] = []
        if not isinstance(data.get("posted_events"), list):
            data["posted_events"] = []
        return data
    except Exception:
        logger.exception("Failed to load feedback weekly stats %s; resetting", path)
        return _default_doc()


def save_stats(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")


def _counter_key(realm: str, stream: str, bucket_id: str) -> tuple[str, str, str]:
    return (str(realm).lower(), str(stream), str(bucket_id))


def _find_or_add_counter(
    doc: dict[str, Any],
    *,
    realm: str,
    stream: str,
    bucket_id: str,
    title: str,
    kind: str,
) -> dict[str, Any]:
    key = _counter_key(realm, stream, bucket_id)
    for row in doc["counters"]:
        if not isinstance(row, dict):
            continue
        if _counter_key(row.get("realm", ""), row.get("stream", ""), row.get("bucket_id", "")) == key:
            row.setdefault("title", title)
            row.setdefault("kind", kind)
            row.setdefault("enqueued", 0)
            row.setdefault("posted", 0)
            return row
    row = {
        "realm": key[0],
        "stream": key[1],
        "bucket_id": key[2],
        "title": title,
        "kind": kind,
        "enqueued": 0,
        "posted": 0,
    }
    doc["counters"].append(row)
    return row


def record_enqueued(
    config_path: Path,
    zulip_cfg: dict[str, Any],
    *,
    realm: str,
    stream: str,
    bucket_id: str,
    title: str,
    kind: str,
    dryrun: bool,
) -> None:
    if dryrun:
        return
    path = feedback_weekly_stats_path(config_path, zulip_cfg)
    try:
        doc = load_stats(path)
        row = _find_or_add_counter(
            doc, realm=realm, stream=stream, bucket_id=bucket_id, title=title, kind=kind
        )
        row["enqueued"] = int(row.get("enqueued") or 0) + 1
        save_stats(path, doc)
    except Exception:
        logger.exception("Failed to record feedback weekly enqueued stats")


def record_posted(
    config_path: Path,
    zulip_cfg: dict[str, Any],
    *,
    realm: str,
    stream: str,
    bucket_id: str,
    title: str,
    kind: str,
    link: str,
    dryrun: bool,
    ts: float | None = None,
) -> None:
    if dryrun:
        return
    path = feedback_weekly_stats_path(config_path, zulip_cfg)
    now = float(ts if ts is not None else time.time())
    try:
        doc = load_stats(path)
        row = _find_or_add_counter(
            doc, realm=realm, stream=stream, bucket_id=bucket_id, title=title, kind=kind
        )
        row["posted"] = int(row.get("posted") or 0) + 1
        events = doc.setdefault("posted_events", [])
        if not isinstance(events, list):
            events = []
            doc["posted_events"] = events
        events.append(
            {
                "link": normalize_link(link),
                "bucket_id": bucket_id,
                "realm": str(realm).lower(),
                "stream": str(stream),
                "ts": now,
            }
        )
        _trim_posted_events(doc)
        save_stats(path, doc)
    except Exception:
        logger.exception("Failed to record feedback weekly posted stats")


def _trim_posted_events(doc: dict[str, Any]) -> None:
    events = doc.get("posted_events") or []
    if not isinstance(events, list):
        doc["posted_events"] = []
        return
    period_start = float(doc.get("period_start_unix") or 0.0)
    cutoff = period_start - EVENT_RETENTION_PAD_SEC if period_start > 0 else 0.0
    kept: list[dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        try:
            ts = float(ev.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if cutoff and ts < cutoff:
            continue
        kept.append(ev)
    if len(kept) > MAX_POSTED_EVENTS:
        kept = kept[-MAX_POSTED_EVENTS:]
    doc["posted_events"] = kept


def reset_period_after_summary(
    config_path: Path,
    zulip_cfg: dict[str, Any],
    *,
    dryrun: bool,
    now: float | None = None,
) -> None:
    if dryrun:
        return
    path = feedback_weekly_stats_path(config_path, zulip_cfg)
    try:
        ts = float(now if now is not None else time.time())
        doc = _default_doc(period_start=ts)
        save_stats(path, doc)
    except Exception:
        logger.exception("Failed to reset feedback weekly stats period")


def counters_for_stream(
    doc: dict[str, Any],
    *,
    realm: str,
    stream: str,
    allowed_bucket_ids: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    r = realm.lower()
    out: list[dict[str, Any]] = []
    for row in doc.get("counters") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("realm", "")).lower() != r or str(row.get("stream", "")) != stream:
            continue
        bid = str(row.get("bucket_id") or "")
        if allowed_bucket_ids is not None and bid not in allowed_bucket_ids:
            continue
        out.append(row)
    out.sort(key=lambda row: (0 if str(row.get("bucket_id", "")).startswith("c:") else 1, row.get("bucket_id", "")))
    return out


def aggregate_votes_for_stream(
    messages: list[dict[str, Any]],
    posted_events: list[dict[str, Any]],
    *,
    realm: str,
    stream: str,
    period_start_unix: float,
) -> dict[str, tuple[int, int]]:
    """Map bucket_id -> (up, down) for reacted bot posts in this stream/period."""
    r = realm.lower()
    link_to_bucket: dict[str, str] = {}
    for ev in posted_events:
        if not isinstance(ev, dict):
            continue
        if str(ev.get("realm", "")).lower() != r or str(ev.get("stream", "")) != stream:
            continue
        link = normalize_link(str(ev.get("link") or ""))
        bid = str(ev.get("bucket_id") or "")
        if link and bid:
            link_to_bucket[link] = bid

    votes: dict[str, list[int]] = {}
    cutoff = float(period_start_unix or 0.0)
    for msg in messages:
        ts = _normalize_message_ts(msg.get("timestamp") or 0)
        if cutoff and ts < cutoff:
            continue
        link = parse_feedback_link_from_body(str(msg.get("content") or ""))
        if not link:
            continue
        bid = link_to_bucket.get(normalize_link(link))
        if not bid:
            continue
        up, down = count_thumbs_reactions(msg)
        if up + down <= 0:
            continue
        cur = votes.setdefault(bid, [0, 0])
        cur[0] += up
        cur[1] += down
    return {k: (v[0], v[1]) for k, v in votes.items()}


def stats_nonzero(
    counters: list[dict[str, Any]],
    votes: dict[str, tuple[int, int]],
) -> bool:
    for row in counters:
        if int(row.get("enqueued") or 0) > 0 or int(row.get("posted") or 0) > 0:
            return True
    for up, down in votes.values():
        if up + down > 0:
            return True
    return False


def _section_heading(kind: str, title: str) -> str:
    if kind == "category":
        return f"### Category `{title}`\n"
    return f"### Group `{title}`\n"


def format_stats_bullets(
    *,
    enqueued: int,
    posted: int,
    votes: tuple[int, int] | None,
) -> list[str]:
    lines = [
        f"- **Queued:** {enqueued}\n",
        f"- **Posted:** {posted}\n",
    ]
    if votes is not None:
        up, down = votes
        lines.append(f"- **Votes:** ↑{up} / ↓{down}\n")
    return lines


def collect_stats_by_bucket(
    counters: list[dict[str, Any]],
    votes: dict[str, tuple[int, int]],
) -> dict[str, dict[str, Any]]:
    """bucket_id -> {title, kind, enqueued, posted, votes|None} for buckets with any signal."""
    by: dict[str, dict[str, Any]] = {}
    for row in counters:
        bid = str(row.get("bucket_id") or "")
        if not bid:
            continue
        enq = int(row.get("enqueued") or 0)
        posted = int(row.get("posted") or 0)
        v = votes.get(bid)
        if enq <= 0 and posted <= 0 and (v is None or v[0] + v[1] <= 0):
            continue
        by[bid] = {
            "title": str(row.get("title") or bid),
            "kind": str(row.get("kind") or "group"),
            "enqueued": enq,
            "posted": posted,
            "votes": v,
        }
    for bid, (up, down) in votes.items():
        if up + down <= 0:
            continue
        if bid in by:
            by[bid]["votes"] = (up, down)
            continue
        # Vote-only (counter row missing): derive title/kind from bucket_id.
        if bid.startswith("c:"):
            title, kind = bid[2:], "category"
        elif bid.startswith("g:"):
            title, kind = bid[2:], "group"
        else:
            title, kind = bid, "group"
        by[bid] = {
            "title": title,
            "kind": kind,
            "enqueued": 0,
            "posted": 0,
            "votes": (up, down),
        }
    return by


def markdown_stats_only(stats_by_bucket: dict[str, dict[str, Any]]) -> str:
    if not stats_by_bucket:
        return ""
    lines: list[str] = []
    for bid in sorted(stats_by_bucket.keys(), key=lambda b: (0 if b.startswith("c:") else 1, b)):
        s = stats_by_bucket[bid]
        lines.append(_section_heading(str(s["kind"]), str(s["title"])))
        lines.extend(
            format_stats_bullets(
                enqueued=int(s["enqueued"]),
                posted=int(s["posted"]),
                votes=s.get("votes"),
            )
        )
        lines.append("\n")
    return "".join(lines).strip()
