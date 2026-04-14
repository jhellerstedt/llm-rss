"""Suggest missing journal domains based on Zulip-linked articles."""
from __future__ import annotations

import logging
from typing import Any

from api_usage import record_zulip_api
from zulip_context import _client_for_realm, domain_from_url, extract_urls_from_zulip_message_content
from zulip_feedback import unique_realm_stream_pairs

logger = logging.getLogger(__name__)

JOURNAL_SUGGESTIONS_TOPIC = "journal suggestions"

# Not journals; common link routers / social / aggregators.
DEFAULT_DOMAIN_DENYLIST: set[str] = {
    "arxiv.org",
    "doi.org",
    "dx.doi.org",
    "x.com",
    "twitter.com",
    "bsky.app",
    "news.ycombinator.com",
    "reddit.com",
    "github.com",
    "youtube.com",
}


def tracked_domains_from_group_urls(urls: list[str]) -> set[str]:
    tracked: set[str] = set()
    for u in urls:
        d = domain_from_url(u)
        if d:
            tracked.add(d)
    return tracked


def domain_counts_from_zulip_messages(
    messages: list[dict[str, Any]],
    *,
    denylist: set[str] | None = None,
) -> dict[str, int]:
    deny = denylist or set()
    counts: dict[str, int] = {}
    for msg in messages:
        raw_html = str(msg.get("content") or "")
        for url in extract_urls_from_zulip_message_content(raw_html):
            d = domain_from_url(url)
            if not d or d in deny:
                continue
            counts[d] = counts.get(d, 0) + 1
    return counts


def missing_domain_counts(
    *,
    tracked_domains: set[str],
    zulip_domain_counts: dict[str, int],
) -> dict[str, int]:
    return {d: c for d, c in zulip_domain_counts.items() if d not in tracked_domains}


def format_missing_journals_message(missing: dict[str, int]) -> str:
    lines = [
        "Untracked journals/sources mentioned in Zulip recently (domain-based):",
        "",
    ]
    for d, c in sorted(missing.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"- {d} (links: {c})")
    lines.append("")
    lines.append("Suggestion: consider adding RSS feeds for these domains under `urls = [...]`.")
    return "\n".join(lines).strip()


def post_missing_journals_suggestions(
    *,
    zulip_sources: list[dict[str, Any]],
    zulip_realms: dict[str, dict[str, str]],
    message: str,
    dryrun: bool,
    topic: str = JOURNAL_SUGGESTIONS_TOPIC,
) -> None:
    if not zulip_sources or not zulip_realms:
        return
    if not message.strip():
        return

    for realm, stream in unique_realm_stream_pairs(zulip_sources):
        try:
            client = _client_for_realm(zulip_realms, realm)
        except Exception:
            logger.exception("Zulip client init failed realm=%s stream=%s", realm, stream)
            continue

        if dryrun:
            logger.info(
                "[dry run] would post journal suggestions realm=%s stream=%s",
                realm,
                stream,
            )
            continue

        try:
            result = client.send_message(
                {
                    "type": "stream",
                    "to": stream,
                    "topic": topic,
                    "content": message,
                }
            )
            if result.get("result") != "success":
                logger.warning(
                    "Zulip send_message failed (journal suggestions) realm=%s stream=%s: %s",
                    realm,
                    stream,
                    result,
                )
                continue
            record_zulip_api(1)
        except Exception:
            logger.exception(
                "Zulip post journal suggestions failed realm=%s stream=%s",
                realm,
                stream,
            )

