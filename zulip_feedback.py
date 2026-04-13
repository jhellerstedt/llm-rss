"""Zulip topic \"feedback ranking\": post recommendations and read reaction signals."""
from __future__ import annotations

import logging
import re
from typing import Any

from rss_merge import normalize_link
from zulip_context import fetch_messages_narrow, strip_zulip_html, _client_for_realm

logger = logging.getLogger(__name__)

FEEDBACK_RANKING_TOPIC = "feedback ranking"
_LINK_LINE = re.compile(r"(?im)^\s*Link:\s*(.+?)\s*$")


def unique_realm_stream_pairs(zulip_sources: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Stable-unique (realm.lower(), stream) from zulip_sources rows."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for src in zulip_sources:
        realm = src.get("realm")
        stream = src.get("stream")
        if not realm or not stream:
            continue
        key = (str(realm).lower(), str(stream))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def lookback_max_for_pair(
    zulip_sources: list[dict[str, Any]], realm: str, stream: str
) -> tuple[int, int]:
    """Max lookback_hours and max_messages among sources matching this realm+stream."""
    r = realm.lower()
    lookbacks: list[int] = []
    max_msgs: list[int] = []
    for src in zulip_sources:
        if str(src.get("realm", "")).lower() != r or str(src.get("stream", "")) != stream:
            continue
        lookbacks.append(int(src.get("lookback_hours", 168)))
        max_msgs.append(int(src.get("max_messages", 500)))
    if not lookbacks:
        return 168, 500
    return max(lookbacks), max(max_msgs)


def parse_feedback_link_from_body(content: str) -> str | None:
    """Extract URL from bot message body (line \"Link: ...\")."""
    raw = strip_zulip_html(content or "")
    m = _LINK_LINE.search(raw)
    if not m:
        return None
    url = m.group(1).strip().strip("<>")
    return url or None


def count_thumbs_reactions(message: dict[str, Any]) -> tuple[int, int]:
    """Count thumbs_up / thumbs_down on a Zulip message dict."""
    reactions = message.get("reactions") or []
    if not isinstance(reactions, list):
        return 0, 0
    up = down = 0
    for r in reactions:
        if not isinstance(r, dict):
            continue
        name = r.get("emoji_name")
        if name == "thumbs_up":
            up += 1
        elif name == "thumbs_down":
            down += 1
    return up, down


def aggregate_feedback_signals(
    messages: list[dict[str, Any]],
) -> dict[str, tuple[int, int]]:
    """Map normalize_link(url) -> (thumbs_up_count, thumbs_down_count)."""
    out: dict[str, tuple[int, int]] = {}
    for msg in messages:
        link = parse_feedback_link_from_body(str(msg.get("content") or ""))
        if not link:
            continue
        key = normalize_link(link)
        u, d = count_thumbs_reactions(msg)
        ou, od = out.get(key, (0, 0))
        out[key] = (ou + u, od + d)
    return out


def links_announced_in_messages(messages: list[dict[str, Any]]) -> set[str]:
    """Normalized links already present in feedback-topic bodies."""
    keys: set[str] = set()
    for msg in messages:
        link = parse_feedback_link_from_body(str(msg.get("content") or ""))
        if link:
            keys.add(normalize_link(link))
    return keys


def merge_signal_maps(
    a: dict[str, tuple[int, int]], b: dict[str, tuple[int, int]]
) -> dict[str, tuple[int, int]]:
    out = dict(a)
    for k, (u, d) in b.items():
        ou, od = out.get(k, (0, 0))
        out[k] = (ou + u, od + d)
    return out


def format_feedback_prompt_snippet(
    article_link: str, signals: dict[str, tuple[int, int]]
) -> str:
    """Short section for the scoring prompt, or empty if no prior feedback row."""
    key = normalize_link(article_link)
    if key not in signals:
        return ""
    up, down = signals[key]
    return (
        "\n### Team feedback on prior recommendations (Zulip)\n"
        "This paper's URL was previously posted in your stream's \"feedback ranking\" topic. "
        f"Emoji reactions recorded there: thumbs_up={up}, thumbs_down={down}. "
        "Treat this as a weak signal from your team (not a hard rule): favor alignment with "
        "thumbs up, discount slightly if thumbs down dominated, but still judge title and "
        "abstract on their merits.\n"
    )


def format_feedback_post_body(title: str, link: str) -> str:
    return f"{title.strip()}\n\nLink: {link.strip()}"


def load_feedback_state_for_group(
    zulip_sources: list[dict[str, Any]],
    zulip_realms: dict[str, dict[str, str]],
) -> tuple[dict[str, tuple[int, int]], dict[tuple[str, str], list[dict[str, Any]]]]:
    """One fetch per unique (realm, stream): merged reaction signals and raw messages per pair."""
    merged: dict[str, tuple[int, int]] = {}
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for realm, stream in unique_realm_stream_pairs(zulip_sources):
        lookback, max_msg = lookback_max_for_pair(zulip_sources, realm, stream)
        try:
            client = _client_for_realm(zulip_realms, realm)
        except KeyError as e:
            logger.error("%s", e)
            continue
        try:
            msgs = fetch_messages_narrow(
                client, stream, FEEDBACK_RANKING_TOPIC, lookback, max_msg
            )
        except Exception:
            logger.exception(
                "Zulip feedback fetch failed realm=%s stream=%s", realm, stream
            )
            continue
        by_pair[(realm, stream)] = msgs
        merged = merge_signal_maps(merged, aggregate_feedback_signals(msgs))
    return merged, by_pair


def post_feedback_ranking_for_new_items(
    zulip_sources: list[dict[str, Any]],
    zulip_realms: dict[str, dict[str, str]],
    *,
    messages_by_pair: dict[tuple[str, str], list[dict[str, Any]]],
    titles_and_links: list[tuple[str, str]],
    dryrun: bool,
) -> None:
    """Post one message per (realm, stream, article) for items not already in that topic."""
    for realm, stream in unique_realm_stream_pairs(zulip_sources):
        msgs = messages_by_pair.get((realm, stream), [])
        posted = links_announced_in_messages(msgs)
        try:
            client = _client_for_realm(zulip_realms, realm)
        except KeyError as e:
            logger.error("%s", e)
            continue
        for title, link in titles_and_links:
            key = normalize_link(link)
            if key in posted:
                continue
            body = format_feedback_post_body(title, link)
            if dryrun:
                logger.info(
                    "[dry run] would post feedback ranking realm=%s stream=%s link=%s",
                    realm,
                    stream,
                    key[:80],
                )
                posted.add(key)
                continue
            try:
                result = client.send_message(
                    {
                        "type": "stream",
                        "to": stream,
                        "topic": FEEDBACK_RANKING_TOPIC,
                        "content": body,
                    }
                )
                if result.get("result") != "success":
                    logger.warning(
                        "Zulip send_message failed realm=%s stream=%s: %s",
                        realm,
                        stream,
                        result,
                    )
                    continue
                posted.add(key)
            except Exception:
                logger.exception(
                    "Zulip post feedback failed realm=%s stream=%s", realm, stream
                )
