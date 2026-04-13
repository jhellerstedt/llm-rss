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


def normalize_link(url: str) -> str:
    u = urlparse(url.strip())
    host = u.netloc.lower()
    path = (u.path or "/").rstrip("/") or "/"
    return urlunparse((u.scheme.lower(), host, path, "", u.query, ""))


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
