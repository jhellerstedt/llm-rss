"""Fetch Zulip message text for RSS curation context (multi-realm)."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_HTML_TAG = re.compile(r"<[^>]+>")


def strip_zulip_html(content: str) -> str:
    s = _HTML_TAG.sub("", content)
    return (
        s.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )


def load_zulip_realms(
    config_file: str | None = None,
) -> dict[str, dict[str, str]]:
    """Load realm credentials: name -> {email, api_key, site, bot_name?}."""
    path = config_file or os.environ.get("ZULIP_REALMS_CONFIG_FILE")
    if path and Path(path).is_file():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            logger.info("Loaded %d Zulip realm(s) from %s", len(data), path)
            return data
        raise ValueError(f"Zulip realms file must be a JSON object: {path}")

    default_json = Path("zulip_realms.json")
    if default_json.is_file():
        with open(default_json, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            logger.info("Loaded %d Zulip realm(s) from zulip_realms.json", len(data))
            return data

    realms: dict[str, dict[str, str]] = {}
    realm_names: set[str] = set()
    for key in os.environ:
        if key.startswith("ZULIP_REALM_") and key.endswith("_EMAIL"):
            name = key.replace("ZULIP_REALM_", "").replace("_EMAIL", "").lower()
            realm_names.add(name)
    for name in realm_names:
        up = name.upper()
        email = os.environ.get(f"ZULIP_REALM_{up}_EMAIL")
        api_key = os.environ.get(f"ZULIP_REALM_{up}_API_KEY")
        site = os.environ.get(f"ZULIP_REALM_{up}_SITE")
        if email and api_key and site:
            realms[name] = {
                "email": email,
                "api_key": api_key,
                "site": site,
                "bot_name": os.environ.get(f"ZULIP_REALM_{up}_BOT_NAME", "bot"),
            }
    if realms:
        logger.info("Loaded %d Zulip realm(s) from environment", len(realms))
    return realms


def _normalize_ts(ts: float | int) -> float:
    if ts > 1e12:
        return ts / 1000.0
    return float(ts)


def _client_for_realm(realms: dict[str, dict[str, str]], realm: str):
    import zulip

    if realm not in realms:
        raise KeyError(f"Unknown Zulip realm '{realm}'. Known: {sorted(realms)}")
    c = realms[realm]
    return zulip.Client(email=c["email"], api_key=c["api_key"], site=c["site"])


def fetch_messages_narrow(
    client,
    stream: str,
    topic: str | None,
    lookback_hours: int,
    max_messages: int,
) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    cutoff_ts = cutoff.timestamp()
    narrow: list[dict[str, str]] = [{"operator": "stream", "operand": stream}]
    if topic:
        narrow.append({"operator": "topic", "operand": topic})

    collected: list[dict[str, Any]] = []
    anchor: str | int = "newest"
    num_before = min(max(1, max_messages), 5000)

    while len(collected) < max_messages:
        req = {
            "anchor": anchor,
            "num_before": num_before,
            "num_after": 0,
            "narrow": narrow,
        }
        result = client.get_messages(req)
        if result.get("result") != "success":
            logger.warning("Zulip get_messages failed: %s", result.get("msg", result))
            break
        messages = result.get("messages") or []
        if not messages:
            break
        for msg in messages:
            ts = _normalize_ts(msg.get("timestamp") or 0)
            if ts >= cutoff_ts:
                collected.append(msg)
        if len(collected) >= max_messages:
            break
        oldest = min(messages, key=lambda m: m.get("id", 0))
        anchor = oldest.get("id")
        if len(messages) < num_before:
            break
        # stop if entire batch is older than cutoff
        newest_ts = max(_normalize_ts(m.get("timestamp") or 0) for m in messages)
        if newest_ts < cutoff_ts:
            break

    collected.sort(key=lambda m: m.get("timestamp", 0))
    return collected[:max_messages]


def format_messages(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for msg in messages:
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        content = strip_zulip_html(content)
        sender = msg.get("sender_full_name") or msg.get("sender_email") or "Unknown"
        ts = msg.get("timestamp")
        if ts:
            tsn = _normalize_ts(ts)
            date_str = datetime.fromtimestamp(tsn, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            lines.append(f"[{date_str}] {sender}: {content}")
        else:
            lines.append(f"{sender}: {content}")
    return "\n---\n".join(lines)


def build_zulip_context_block(
    zulip_sources: list[dict[str, Any]],
    realms: dict[str, dict[str, str]],
    context_max_chars: int,
    kagi_summarize: Any | None = None,
) -> str:
    """Concatenate formatted messages from all sources; summarize if over context_max_chars."""
    if not zulip_sources:
        return ""

    try:
        import zulip  # noqa: F401
    except ImportError as e:
        raise ImportError("Install the 'zulip' package when using zulip_sources") from e

    parts: list[str] = []
    for src in zulip_sources:
        realm = src.get("realm")
        stream = src.get("stream")
        if not realm or not stream:
            logger.warning("Skipping zulip source missing realm or stream: %s", src)
            continue
        topic = src.get("topic")
        lookback = int(src.get("lookback_hours", 168))
        max_msg = int(src.get("max_messages", 500))
        try:
            client = _client_for_realm(realms, str(realm).lower())
        except KeyError as e:
            logger.error("%s", e)
            continue
        try:
            msgs = fetch_messages_narrow(client, stream, topic, lookback, max_msg)
        except Exception:
            logger.exception("Zulip fetch failed realm=%s stream=%s", realm, stream)
            continue
        label = f"{realm}/{stream}" + (f"/{topic}" if topic else "")
        body = format_messages(msgs)
        if body:
            parts.append(f"### {label}\n{body}")

    raw = "\n\n".join(parts).strip()
    if not raw:
        return ""

    if len(raw) <= context_max_chars or kagi_summarize is None:
        return raw[:context_max_chars] if len(raw) > context_max_chars else raw

    try:
        digest = kagi_summarize.summarize(raw[:200_000])
        return f"(Summarized from Zulip; original length {len(raw)} chars.)\n\n{digest}"
    except Exception:
        logger.exception("Kagi summarize failed; truncating Zulip context")
        return raw[:context_max_chars]
