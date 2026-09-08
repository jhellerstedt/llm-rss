"""Merge newly scored RSS items with a previously written feed for stable history."""

from __future__ import annotations

import logging
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import feedparser

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    description: str
    pubdate: datetime
    unique_id: str


LinkScore = tuple[str, int, int]  # link, relevance, impact


@dataclass
class GroupPassingScores:
    """Passing papers from one config group, for cross-group link assignment."""

    group_name: str
    link_scores: list[LinkScore]
    identity_keys_by_link: dict[str, set[str]] | None = None


def normalize_link(url: str) -> str:
    u = urlparse(url.strip())
    host = u.netloc.lower()
    path = (u.path or "/").rstrip("/") or "/"
    return urlunparse((u.scheme.lower(), host, path, "", u.query, ""))


def winning_group_by_link(batches: list[GroupPassingScores]) -> dict[str, str]:
    """Map normalized link -> group that owns it (highest relevance, then impact).

    Links whose identity key sets intersect (DOI / arXiv / URL) share one winner.
    """
    from paper_identity import cluster_links_by_identity, identity_keys

    best: dict[str, tuple[int, int, str]] = {}
    keysets: dict[str, set[str]] = {}
    for batch in batches:
        extra = batch.identity_keys_by_link or {}
        for link, rel, imp in batch.link_scores:
            k = normalize_link(link)
            ikeys = extra.get(link) or extra.get(k)
            if not ikeys:
                ikeys = identity_keys(link)
            keysets.setdefault(k, set()).update(ikeys)
            keysets[k].add("url:" + k)
            cur = best.get(k)
            if cur is None:
                best[k] = (rel, imp, batch.group_name)
                continue
            if rel > cur[0] or (rel == cur[0] and imp > cur[1]):
                best[k] = (rel, imp, batch.group_name)
            elif rel == cur[0] and imp == cur[1] and batch.group_name < cur[2]:
                best[k] = (rel, imp, batch.group_name)

    if not best:
        return {}

    winners: dict[str, str] = {k: v[2] for k, v in best.items()}
    for cluster in cluster_links_by_identity(keysets):
        if len(cluster) < 2:
            continue
        ranked = [lnk for lnk in cluster if lnk in best]
        if not ranked:
            continue
        winner_link = ranked[0]
        for lnk in ranked[1:]:
            ra, ia, na = best[winner_link]
            rb, ib, nb = best[lnk]
            if rb > ra or (rb == ra and ib > ia) or (
                rb == ra and ib == ia and nb < na
            ):
                winner_link = lnk
        group = best[winner_link][2]
        for lnk in ranked:
            winners[lnk] = group
    return winners


def filter_feed_items_for_group(
    items: list[FeedItem],
    group_name: str,
    winners: dict[str, str],
) -> list[FeedItem]:
    """Keep items this group owns; drop links assigned to another group this run."""
    return [
        item
        for item in items
        if winners.get(normalize_link(item.link), group_name) == group_name
    ]


def _entry_pubdate(entry: feedparser.FeedParserDict) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return datetime.fromtimestamp(timegm(t), tz=timezone.utc)


def load_persisted_feed_items(rss_path: Path) -> list[FeedItem]:
    if not rss_path.is_file():
        return []
    parsed = feedparser.parse(str(rss_path))
    if getattr(parsed, "bozo", False) and getattr(parsed, "bozo_exception", None):
        logger.warning(
            "RSS parse issue for %s: %s", rss_path, parsed.bozo_exception
        )
    out: list[FeedItem] = []
    for entry in parsed.entries or []:
        link = entry.get("link")
        if not link:
            continue
        title = entry.get("title") or ""
        desc = entry.get("summary") or entry.get("description") or ""
        pub = _entry_pubdate(entry)
        if pub is None:
            logger.warning("Skipping persisted entry without date: %s", link)
            continue
        uid = str(entry.get("id") or link)
        out.append(
            FeedItem(
                title=title,
                link=str(link),
                description=desc,
                pubdate=pub,
                unique_id=uid,
            )
        )
    return out


def merge_feed_history(
    persisted: list[FeedItem],
    new_items: list[FeedItem],
    max_items: int,
) -> list[FeedItem]:
    """Merge persisted history with items from this run.

    New links are added. If a link already exists, the entry from ``new_items``
    replaces title/description/link for that key so re-passing papers pick up
    updated scores and metadata (e.g. OpenAlex h-index), while ``pubdate``
    and ``unique_id`` stay the same as the first persisted copy for stable
    ordering and feed reader identity.
    """
    by_key: dict[str, FeedItem] = {}
    for item in persisted:
        by_key.setdefault(normalize_link(item.link), item)
    for item in new_items:
        key = normalize_link(item.link)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = item
        else:
            by_key[key] = FeedItem(
                title=item.title,
                link=item.link,
                description=item.description,
                pubdate=existing.pubdate,
                unique_id=existing.unique_id,
            )
    merged = sorted(
        by_key.values(),
        key=lambda i: i.pubdate,
        reverse=True,
    )
    return merged[: max(0, max_items)]
