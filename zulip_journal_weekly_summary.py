"""Post a weekly Zulip digest of config.toml changes under topic \"journal suggestions\"."""
from __future__ import annotations

import copy
import json
import logging
import time
from pathlib import Path
from typing import Any

from api_usage import record_zulip_api
from zulip_context import _client_for_realm, fetch_messages_narrow
from zulip_feedback import unique_realm_stream_pairs

logger = logging.getLogger(__name__)

JOURNAL_SUGGESTIONS_TOPIC = "journal suggestions"
SUMMARY_INTERVAL_SEC = 7 * 24 * 3600
ZULIP_MESSAGE_MAX_CHARS = 9500


def _state_path(config_path: Path) -> Path:
    return config_path.with_name(f"{config_path.stem}.journal_weekly_summary_state.json")


def _normalize_feed_category(value: object) -> str | None:
    """Single token from config (first word), matching main._normalize_feed_category."""
    s = str(value or "").strip()
    if not s:
        return None
    return s.split()[0]


def _resolved_feed_category(group: dict, cfg: dict) -> str | None:
    return _normalize_feed_category(
        group.get("feed_category")
        or group.get("category")
        or cfg.get("feed_category")
        or cfg.get("category")
    )


def _normalize_cfg_snapshot(cfg: dict) -> dict[str, Any]:
    if cfg.get("groups"):
        return {
            "mode": "groups",
            "groups": [
                {
                    "name": g.get("name", "unnamed"),
                    "feed_category": _resolved_feed_category(g, cfg),
                    "urls": list(g.get("urls") or []),
                    "research_areas": list(g.get("research_areas") or []),
                    "excluded_areas": list(g.get("excluded_areas") or []),
                }
                for g in cfg["groups"]
            ],
        }
    return {
        "mode": "legacy",
        "feed_category": _normalize_feed_category(
            cfg.get("feed_category") or cfg.get("category")
        ),
        "urls": list(cfg.get("urls") or []),
        "research_areas": list(cfg.get("research_areas") or []),
        "excluded_areas": list(cfg.get("excluded_areas") or []),
    }


def _bucket_id_title_for_group(g: dict, cfg: dict) -> tuple[str, str, str]:
    """Return (bucket_id, markdown_title, heading_kind) with heading_kind \"category\" or \"group\"."""
    fc = g.get("feed_category")
    if fc is None:
        fc = _resolved_feed_category(g, cfg)
    name = str(g.get("name") or "unnamed")
    if fc:
        return f"c:{fc}", str(fc), "category"
    return f"g:{name}", name, "group"


def _aggregate_group_buckets(snapshot: dict) -> dict[str, dict[str, Any]]:
    """bucket_id -> {title, kind, feed_urls set, keyword_lines int}."""
    buckets: dict[str, dict[str, Any]] = {}
    cfg = snapshot
    for g in snapshot["groups"]:
        bid, title, kind = _bucket_id_title_for_group(g, cfg)
        urls = set(g.get("urls") or [])
        ra = g.get("research_areas") or []
        ea = g.get("excluded_areas") or []
        kw = len(ra) + len(ea)
        if bid not in buckets:
            buckets[bid] = {"title": title, "kind": kind, "urls": set(), "kw": 0}
        buckets[bid]["urls"].update(urls)
        buckets[bid]["kw"] += kw
    for b in buckets.values():
        b["feeds"] = len(b["urls"])
    return buckets


def _format_delta(n: int) -> str:
    if n == 0:
        return "0"
    sign = "+" if n > 0 else ""
    return f"{sign}{n}"


def _section_heading(kind: str, title: str) -> str:
    if kind == "category":
        return f"### Category `{title}`\n"
    return f"### Group `{title}`\n"


def _backfill_group_feed_categories(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Old weekly-summary state files lack per-group feed_category; align buckets with `after` by name."""
    if before.get("mode") != "groups":
        return before
    out = copy.deepcopy(before)
    ref_by = {str(g.get("name")): g for g in after.get("groups") or []}
    for g in out["groups"]:
        if g.get("feed_category") is None:
            rg = ref_by.get(str(g.get("name")))
            if rg is not None and rg.get("feed_category"):
                g["feed_category"] = rg["feed_category"]
    return out


def _markdown_groups_compact(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    allowed_bucket_ids: frozenset[str] | None = None,
) -> str:
    before = _backfill_group_feed_categories(before, after)
    b_ag = _aggregate_group_buckets(before)
    a_ag = _aggregate_group_buckets(after)
    lines: list[str] = []
    all_ids = sorted(set(b_ag) | set(a_ag), key=lambda bid: (0 if bid.startswith("c:") else 1, bid))
    changed = False
    for bid in all_ids:
        if allowed_bucket_ids is not None and bid not in allowed_bucket_ids:
            continue
        b = b_ag.get(bid)
        a = a_ag.get(bid)
        if b is None and a is None:
            continue
        kind = (a or b)["kind"]
        title = (a or b)["title"]
        feeds_b = len(b["urls"]) if b else 0
        feeds_a = len(a["urls"]) if a else 0
        kw_b = b["kw"] if b else 0
        kw_a = a["kw"] if a else 0
        urls_b = frozenset(b["urls"]) if b else frozenset()
        urls_a = frozenset(a["urls"]) if a else frozenset()
        added_n = len(urls_a - urls_b)
        removed_n = len(urls_b - urls_a)
        d_feeds = feeds_a - feeds_b
        d_kw = kw_a - kw_b
        if (
            d_feeds == 0
            and d_kw == 0
            and added_n == 0
            and removed_n == 0
            and b is not None
            and a is not None
        ):
            continue
        changed = True
        lines.append(_section_heading(kind, title))
        lines.append(f"- **Journal feeds:** {feeds_a} (Δ {_format_delta(d_feeds)} since last summary)\n")
        lines.append(
            f"- **Keywords** (research + excluded lines): {kw_a} "
            f"(Δ {_format_delta(d_kw)} since last summary)\n"
        )
        if added_n or removed_n:
            parts = []
            if added_n:
                parts.append(f"**{added_n}** RSS URL(s) added")
            if removed_n:
                parts.append(f"**{removed_n}** RSS URL(s) removed")
            lines.append(f"- {'; '.join(parts)}\n")
        lines.append("\n")
    if not changed:
        return ""
    return "\n".join(lines).strip()


def _markdown_legacy_compact(before: dict[str, Any], after: dict[str, Any]) -> str:
    urls_b = set(before.get("urls") or [])
    urls_a = set(after.get("urls") or [])
    ra_b, ea_b = before.get("research_areas") or [], before.get("excluded_areas") or []
    ra_a, ea_a = after.get("research_areas") or [], after.get("excluded_areas") or []
    kw_b = len(ra_b) + len(ea_b)
    kw_a = len(ra_a) + len(ea_a)
    feeds_b, feeds_a = len(urls_b), len(urls_a)
    d_feeds = feeds_a - feeds_b
    d_kw = kw_a - kw_b
    added_n = len(urls_a - urls_b)
    removed_n = len(urls_b - urls_a)
    if d_feeds == 0 and d_kw == 0 and added_n == 0 and removed_n == 0:
        return ""
    fc = after.get("feed_category") or before.get("feed_category")
    if fc:
        lines = [_section_heading("category", str(fc))]
    else:
        lines = ["### Legacy config (single block)\n"]
    lines.append(f"- **Journal feeds:** {feeds_a} (Δ {_format_delta(d_feeds)} since last summary)\n")
    lines.append(
        f"- **Keywords** (research + excluded lines): {kw_a} "
        f"(Δ {_format_delta(d_kw)} since last summary)\n"
    )
    if added_n or removed_n:
        parts = []
        if added_n:
            parts.append(f"**{added_n}** RSS URL(s) added")
        if removed_n:
            parts.append(f"**{removed_n}** RSS URL(s) removed")
        lines.append(f"- {'; '.join(parts)}\n")
    return "".join(lines).strip()


def markdown_config_diff(
    before: dict[str, Any] | None,
    after: dict[str, Any],
    *,
    allowed_bucket_ids: frozenset[str] | None = None,
) -> str:
    """Human-readable markdown of differences between two normalized snapshots.

    For ``[[groups]]`` snapshots, ``allowed_bucket_ids`` limits sections to buckets
    (category or per-group) that use the target Zulip stream in ``zulip_sources``.
    """
    if before is None:
        return ""
    if before.get("mode") != after.get("mode"):
        return (
            "- **Config layout changed** (switched between legacy top-level keys and `[[groups]]`). "
            "Review `config.toml` manually.\n"
        )

    if after["mode"] == "legacy":
        return _markdown_legacy_compact(before, after)

    return _markdown_groups_compact(
        before, after, allowed_bucket_ids=allowed_bucket_ids
    )


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("Failed to load journal weekly summary state %s", path)
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def _get_bot_user_id(client) -> int | None:
    try:
        prof = client.get_profile()
        record_zulip_api(1)
    except Exception:
        logger.exception("Zulip get_profile failed")
        return None
    if prof.get("result") != "success":
        logger.warning("Zulip get_profile not success: %s", prof)
        return None
    uid = prof.get("user_id")
    return int(uid) if uid is not None else None


def newest_bot_message_timestamp_in_topic(
    client,
    *,
    stream: str,
    topic: str,
    bot_user_id: int,
    max_messages: int = 500,
) -> float | None:
    """Unix timestamp (seconds) of the bot's newest message in stream/topic, or None."""
    try:
        msgs = fetch_messages_narrow(
            client,
            stream,
            topic,
            lookback_hours=24 * 120,
            max_messages=max_messages,
        )
    except Exception:
        logger.exception("Zulip fetch journal-suggestions topic failed stream=%s", stream)
        return None
    best: float | None = None
    for m in msgs:
        if int(m.get("sender_id") or 0) != bot_user_id:
            continue
        ts = m.get("timestamp")
        if ts is None:
            continue
        tsn = float(ts)
        if tsn > 1e12:
            tsn /= 1000.0
        if best is None or tsn > best:
            best = tsn
    return best


def _realm_stream_pairs_for_summary(cfg: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    if cfg.get("groups"):
        for g in cfg["groups"]:
            for pair in unique_realm_stream_pairs(g.get("zulip_sources") or []):
                if pair not in seen:
                    seen.add(pair)
                    out.append(pair)
    else:
        z = cfg.get("zulip_sources") or []
        for pair in unique_realm_stream_pairs(z):
            if pair not in seen:
                seen.add(pair)
                out.append(pair)
    return out


def _allowed_bucket_ids_for_realm_stream(
    cfg: dict[str, Any], realm: str, stream: str
) -> frozenset[str] | None:
    """Bucket ids for `[[groups]]` rows that post journal suggestions to this stream.

    Returns ``None`` for legacy top-level config (weekly body is not bucket-filtered).
    """
    if not cfg.get("groups"):
        return None
    r = realm.lower()
    ids: set[str] = set()
    for g in cfg["groups"]:
        for pr, ps in unique_realm_stream_pairs(g.get("zulip_sources") or []):
            if pr == r and ps == stream:
                bid, _, _ = _bucket_id_title_for_group(g, cfg)
                ids.add(bid)
                break
    return frozenset(ids)


def maybe_post_weekly_journal_config_summary(
    *,
    config_path: Path,
    cfg: dict,
    zulip_realms: dict[str, dict[str, str]],
    zulip_cfg: dict,
    dryrun: bool,
) -> None:
    """If due (~weekly) and the config changed since the saved baseline, post a Zulip summary."""
    if zulip_cfg.get("journal_weekly_summary") is False:
        return
    if not zulip_realms:
        return
    pairs = _realm_stream_pairs_for_summary(cfg)
    if not pairs:
        return

    state_path = _state_path(config_path)
    state = _load_state(state_path)
    current = _normalize_cfg_snapshot(cfg)
    snap = state.get("snap")

    if snap is None:
        if dryrun:
            logger.info(
                "[dry run] journal weekly summary: would save initial snapshot for %s",
                config_path.name,
            )
            return
        state["snap"] = current
        state["last_summary_post_unix"] = time.time()
        _save_state(state_path, state)
        logger.info(
            "Journal weekly summary: saved initial config snapshot for %s (no post yet)",
            config_path.name,
        )
        return

    now = time.time()
    last_local = float(state.get("last_summary_post_unix") or 0.0)

    zulip_last_ts: float | None = None
    for realm, stream in pairs:
        try:
            client = _client_for_realm(zulip_realms, realm)
        except Exception:
            logger.exception("Zulip client init failed realm=%s", realm)
            continue
        bot_id = _get_bot_user_id(client)
        if bot_id is None:
            continue
        ts = newest_bot_message_timestamp_in_topic(
            client, stream=stream, topic=JOURNAL_SUGGESTIONS_TOPIC, bot_user_id=bot_id
        )
        if ts is not None and (zulip_last_ts is None or ts > zulip_last_ts):
            zulip_last_ts = ts

    # Prefer the later of local last-post time and the newest bot message in the topic
    # so we respect an existing conversation baseline.
    baseline_last = max(last_local, zulip_last_ts or 0.0)
    if now - baseline_last < SUMMARY_INTERVAL_SEC:
        return

    body_full = markdown_config_diff(snap, current)
    if not body_full:
        if dryrun:
            return
        state["snap"] = current
        state["last_summary_post_unix"] = now
        _save_state(state_path, state)
        logger.info(
            "Journal weekly summary: interval elapsed for %s but no config diff; "
            "refreshed snapshot and timer",
            config_path.name,
        )
        return

    from datetime import datetime, timezone

    since_s = baseline_last if baseline_last > 0 else last_local
    since_dt = datetime.fromtimestamp(since_s, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = (
        f"## Weekly `config.toml` summary — `{config_path.name}`\n\n"
        f"_Changes since about **{since_dt}** (previous summary post or baseline)._\n\n"
    )

    streams_with_relevant_updates = 0
    for realm, stream in pairs:
        allowed = _allowed_bucket_ids_for_realm_stream(cfg, realm, stream)
        if allowed is None:
            stream_body = body_full
        else:
            stream_body = markdown_config_diff(
                snap, current, allowed_bucket_ids=allowed
            )
        if not stream_body:
            if dryrun:
                logger.info(
                    "[dry run] journal weekly summary: skip realm=%s stream=%s "
                    "(no diff for this stream's categories/groups)",
                    realm,
                    stream,
                )
            continue
        streams_with_relevant_updates += 1
        message = header + stream_body
        if len(message) > ZULIP_MESSAGE_MAX_CHARS:
            message = message[: ZULIP_MESSAGE_MAX_CHARS - 40] + "\n\n_…(message truncated)_\n"

        try:
            client = _client_for_realm(zulip_realms, realm)
        except Exception:
            logger.exception("Zulip client init failed realm=%s stream=%s", realm, stream)
            continue
        if dryrun:
            logger.info(
                "[dry run] would post journal weekly summary realm=%s stream=%s (%d chars)",
                realm,
                stream,
                len(message),
            )
            continue
        try:
            result = client.send_message(
                {
                    "type": "stream",
                    "to": stream,
                    "topic": JOURNAL_SUGGESTIONS_TOPIC,
                    "content": message,
                }
            )
            record_zulip_api(1)
            if result.get("result") != "success":
                logger.warning(
                    "Zulip weekly summary send failed realm=%s stream=%s: %s",
                    realm,
                    stream,
                    result,
                )
        except Exception:
            logger.exception(
                "Zulip weekly summary post failed realm=%s stream=%s", realm, stream
            )

    if dryrun:
        logger.info(
            "Journal weekly summary dry run for %s: %d stream destination(s) would receive "
            "a relevant digest (of %d configured)",
            config_path.name,
            streams_with_relevant_updates,
            len(pairs),
        )
        return
    state["snap"] = current
    state["last_summary_post_unix"] = time.time()
    _save_state(state_path, state)
    logger.info(
        "Posted journal weekly summary for %s (%d stream destination(s) with relevant updates, "
        "%d configured)",
        config_path.name,
        streams_with_relevant_updates,
        len(pairs),
    )
