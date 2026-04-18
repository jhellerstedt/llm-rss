"""Post a weekly Zulip digest of config.toml changes under topic \"journal suggestions\"."""
from __future__ import annotations

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


def _normalize_cfg_snapshot(cfg: dict) -> dict[str, Any]:
    if cfg.get("groups"):
        return {
            "mode": "groups",
            "groups": [
                {
                    "name": g.get("name", "unnamed"),
                    "urls": list(g.get("urls") or []),
                    "research_areas": list(g.get("research_areas") or []),
                    "excluded_areas": list(g.get("excluded_areas") or []),
                }
                for g in cfg["groups"]
            ],
        }
    return {
        "mode": "legacy",
        "urls": list(cfg.get("urls") or []),
        "research_areas": list(cfg.get("research_areas") or []),
        "excluded_areas": list(cfg.get("excluded_areas") or []),
    }


def _list_added_removed(before: list[str], after: list[str]) -> tuple[list[str], list[str]]:
    b, a = set(before), set(after)
    return sorted(a - b), sorted(b - a)


def markdown_config_diff(before: dict[str, Any] | None, after: dict[str, Any]) -> str:
    """Human-readable markdown of differences between two normalized snapshots."""
    if before is None:
        return ""
    if before.get("mode") != after.get("mode"):
        return (
            "- **Config layout changed** (switched between legacy top-level keys and `[[groups]]`). "
            "Review `config.toml` manually.\n"
        )

    lines: list[str] = []
    if after["mode"] == "legacy":
        for label, key in (
            ("Feed URLs", "urls"),
            ("research_areas", "research_areas"),
            ("excluded_areas", "excluded_areas"),
        ):
            added, removed = _list_added_removed(
                list(before.get(key) or []),
                list(after.get(key) or []),
            )
            if added:
                lines.append(f"- **{label} — added:**\n" + "\n".join(f"  - `{x}`" for x in added))
            if removed:
                lines.append(f"- **{label} — removed:**\n" + "\n".join(f"  - `{x}`" for x in removed))
        return "\n".join(lines).strip()

    before_by = {g["name"]: g for g in before["groups"]}
    after_by = {g["name"]: g for g in after["groups"]}
    names = sorted(set(before_by) | set(after_by))
    for name in names:
        b = before_by.get(name)
        a = after_by.get(name)
        if b is None:
            lines.append(f"### Group `{name}`\n- **New group** in config.\n")
            continue
        if a is None:
            lines.append(f"### Group `{name}`\n- **Group removed** from config.\n")
            continue
        sub: list[str] = []
        for label, key in (
            ("Feed URLs", "urls"),
            ("research_areas", "research_areas"),
            ("excluded_areas", "excluded_areas"),
        ):
            added, removed = _list_added_removed(
                list(b.get(key) or []),
                list(a.get(key) or []),
            )
            if added:
                sub.append(
                    f"- **{label} — added:**\n"
                    + "\n".join(f"  - {json.dumps(x, ensure_ascii=False)}" for x in added)
                )
            if removed:
                sub.append(
                    f"- **{label} — removed:**\n"
                    + "\n".join(f"  - {json.dumps(x, ensure_ascii=False)}" for x in removed)
                )
        if sub:
            lines.append(f"### Group `{name}`\n" + "\n".join(sub) + "\n")
    return "\n".join(lines).strip()


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

    body = markdown_config_diff(snap, current)
    if not body:
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
    message = header + body
    if len(message) > ZULIP_MESSAGE_MAX_CHARS:
        message = message[: ZULIP_MESSAGE_MAX_CHARS - 40] + "\n\n_…(message truncated)_\n"

    for realm, stream in pairs:
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
        return
    state["snap"] = current
    state["last_summary_post_unix"] = time.time()
    _save_state(state_path, state)
    logger.info(
        "Posted journal weekly summary for %s to %d Zulip destination(s)",
        config_path.name,
        len(pairs),
    )
