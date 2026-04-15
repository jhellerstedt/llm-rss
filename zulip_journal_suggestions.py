"""Suggest missing journal domains based on Zulip-linked articles."""
from __future__ import annotations

import logging
import json
from typing import Any

from api_usage import record_zulip_api
from fastgpt_reply import try_load_json_object_from_llm
from journal_venue import (
    VenueBucket,
    bucket_from_ref,
    merge_bucket,
    venue_fallback_host,
    venue_from_article_url,
)
from zulip_context import (
    ZULIP_SECTION_META_KEY,
    _client_for_realm,
    domain_from_url,
    extract_urls_from_zulip_message_content,
)
from zulip_feedback import unique_realm_stream_pairs

logger = logging.getLogger(__name__)

JOURNAL_SUGGESTIONS_TOPIC = "journal suggestions"

UNKNOWN_ZULIP_SECTION = "(unknown section)"

# Max venues listed per Zulip section before collapsing the rest.
MAX_VENUES_PER_SECTION = 15

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


def missing_venues_by_section_from_messages(
    messages: list[dict[str, Any]],
    *,
    tracked_venue_keys: set[str],
    denylist: set[str] | None = None,
) -> dict[str, dict[str, VenueBucket]]:
    """Count untracked venues per Zulip context section (`realm/stream[/topic]`)."""
    deny = denylist or set()
    out: dict[str, dict[str, VenueBucket]] = {}
    for msg in messages:
        section = str(msg.get(ZULIP_SECTION_META_KEY) or "").strip() or UNKNOWN_ZULIP_SECTION
        raw_html = str(msg.get("content") or "")
        for url in extract_urls_from_zulip_message_content(raw_html):
            domain = domain_from_url(url)
            if not domain or domain in deny:
                continue
            ref = venue_from_article_url(url)
            if ref is None:
                ref = venue_fallback_host(url, domain)
            if ref.venue_key in tracked_venue_keys:
                continue
            sec_map = out.setdefault(section, {})
            if ref.venue_key not in sec_map:
                sec_map[ref.venue_key] = bucket_from_ref(ref, url)
            b = sec_map[ref.venue_key]
            b.count += 1
            if not b.example_url:
                b.example_url = url
    return out


def merge_journal_suggestion_maps(
    dest: dict[str, dict[str, VenueBucket]],
    src: dict[str, dict[str, VenueBucket]],
) -> None:
    """Merge venue counts from `src` into `dest` (same section / venue_key sums)."""
    for section, vmap in src.items():
        dsec = dest.setdefault(section, {})
        for vk, b in vmap.items():
            if vk not in dsec:
                dsec[vk] = VenueBucket(
                    count=b.count,
                    display_name=b.display_name,
                    suggested_rss=b.suggested_rss,
                    journal_page_url=b.journal_page_url,
                    apex_domain=b.apex_domain,
                    example_url=b.example_url,
                )
            else:
                merge_bucket(dsec[vk], b)


def apex_domains_from_nested(by_section: dict[str, dict[str, VenueBucket]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for vmap in by_section.values():
        for b in vmap.values():
            d = (b.apex_domain or "").strip().lower().removeprefix("www.")
            if d and d not in seen:
                seen.add(d)
                out.append(d)
    return sorted(out)


def filter_nested_by_allowed_domains(
    by_section: dict[str, dict[str, VenueBucket]],
    allowed_domains: set[str],
) -> dict[str, dict[str, VenueBucket]]:
    allowed = {x.strip().lower().removeprefix("www.") for x in allowed_domains if x.strip()}
    out: dict[str, dict[str, VenueBucket]] = {}
    for sec, vmap in by_section.items():
        kept = {
            vk: b
            for vk, b in vmap.items()
            if (b.apex_domain or "").strip().lower().removeprefix("www.") in allowed
        }
        if kept:
            out[sec] = kept
    return out


def _parse_kagi_journal_domain_filter_response(text: str) -> tuple[set[str], dict[str, str]]:
    """Parse FastGPT JSON response into (allowed_domains, reason_by_domain)."""
    obj = try_load_json_object_from_llm(text or "")
    if not isinstance(obj, dict):
        return set(), {}

    allowed_raw = obj.get("academic_domains") or []
    if not isinstance(allowed_raw, list):
        allowed_raw = []
    allowed: set[str] = {
        str(d).strip().lower().removeprefix("www.") for d in allowed_raw if str(d).strip()
    }

    reasons: dict[str, str] = {}
    reasons_raw = obj.get("reasons") or {}
    if isinstance(reasons_raw, dict):
        for k, v in reasons_raw.items():
            dk = str(k).strip().lower().removeprefix("www.")
            if not dk:
                continue
            reasons[dk] = str(v).strip()
    return allowed, reasons


def filter_academic_journal_domains_with_kagi(
    kagi,
    domains: list[str],
) -> tuple[list[str], dict[str, str]]:
    """One FastGPT call to filter domains to academic journal/publisher sites."""
    uniq: list[str] = []
    seen: set[str] = set()
    for d in domains:
        dd = str(d).strip().lower()
        if dd.startswith("www."):
            dd = dd[4:]
        if dd and dd not in seen:
            seen.add(dd)
            uniq.append(dd)
    if not uniq:
        return [], {}

    prompt = (
        "You are helping curate RSS sources for academic journals.\n"
        "Given this list of web domains, return ONLY the ones that are academic journals or "
        "academic publishers that publish research articles (not preprint servers, DOI resolvers, "
        "social media, code hosting, video sites, aggregators, or general news).\n\n"
        "Return ONLY a single JSON object with keys:\n"
        '- "academic_domains": array of domains to keep (strings)\n'
        '- "reasons": object mapping domain -> short reason (optional)\n\n'
        f"Domains:\n{json.dumps(uniq)}\n"
    )
    text = kagi.fastgpt_query(prompt)
    if not (text or "").strip():
        logger.warning("Kagi journal-domain filter returned empty output")
        return [], {}

    allowed, reasons = _parse_kagi_journal_domain_filter_response(text)
    if not allowed:
        logger.warning(
            "Kagi journal-domain filter parse miss or empty allowlist; snippet=%s",
            (text or "")[:400],
        )
        return [], {}

    kept = [d for d in uniq if d in allowed]
    kept_reasons = {d: reasons.get(d, "") for d in kept if reasons.get(d)}
    return kept, kept_reasons


def format_missing_journals_message(missing: dict[str, int]) -> str:
    """Legacy flat formatter (domain -> count). Prefer `format_missing_journals_message_nested`."""
    lines = [
        "Untracked journals/sources mentioned in Zulip recently (domain-based):",
        "",
    ]
    for d, c in sorted(missing.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"- {d} (links: {c})")
    lines.append("")
    lines.append("Suggestion: consider adding RSS feeds for these domains under `urls = [...]`.")
    return "\n".join(lines).strip()


def format_missing_journals_message_nested(
    by_section: dict[str, dict[str, VenueBucket]],
    *,
    max_per_section: int = MAX_VENUES_PER_SECTION,
) -> str:
    """Markdown grouped by Zulip context section with venue-level RSS hints."""
    lines = [
        "Untracked journals/sources mentioned in Zulip recently (by venue and Zulip section):",
        "",
    ]
    for section in sorted(by_section.keys()):
        vmap = by_section[section]
        lines.append(f"### {section}")
        ranked = sorted(vmap.items(), key=lambda kv: (-kv[1].count, kv[1].display_name))
        shown = ranked[:max_per_section]
        hidden_n = len(ranked) - len(shown)
        for _vk, b in shown:
            tail: list[str] = []
            if b.suggested_rss:
                tail.append(f"add `{b.suggested_rss}`")
            elif b.journal_page_url:
                tail.append(f"journal page: {b.journal_page_url}")
            extra = f" — {' — '.join(tail)}" if tail else ""
            lines.append(f"- **{b.display_name}** — links: {b.count}{extra}")
        if hidden_n > 0:
            lines.append(f"- _… and {hidden_n} more venue(s) in this section._")
        lines.append("")

    lines.append(
        "Suggestion: add these **feed URLs** to the matching `[[groups]]` entry under `urls = [...]` "
        "(or subscribe via another feed if the publisher does not expose RSS)."
    )
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

