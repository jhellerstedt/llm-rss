"""Author whitelist store + matcher.

Articles authored by a whitelisted researcher are always included in feed output,
bypassing the LLM relevance/impact thresholds. Identity is resolved once when an
author is added (see author_resolve.py); scan-time matching is by normalized name
(no network), with an optional OpenAlex-author-id path.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, Field

from adapter import ArticleInfo

logger = logging.getLogger(__name__)

WHITELIST_VERSION = 1


def normalize_author_name(name: str) -> str:
    """Lowercase + collapse whitespace; mirrors openalex_enrich._norm_person_name."""
    t = str(name).strip().lower()
    return re.sub(r"\s+", " ", t)


def split_author_names(authors: str) -> list[str]:
    """Split an RSS author string into individual names."""
    if not authors:
        return []
    parts = re.split(r"\s*(?:,|;|&|\band\b)\s*", authors)
    return [p.strip() for p in parts if p.strip()]


class WhitelistedAuthor(BaseModel):
    id: str
    display_name: str
    name_aliases: list[str] = Field(default_factory=list)
    orcid: str | None = None
    openalex_id: str | None = None
    affiliation: str | None = None
    works_count: int | None = None
    source: str = "manual"
    added_by: str | None = None
    added_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def normalized_aliases(self) -> set[str]:
        out = {normalize_author_name(a) for a in self.name_aliases if a.strip()}
        out.add(normalize_author_name(self.display_name))
        return {a for a in out if a}


class AuthorWhitelist(BaseModel):
    version: int = WHITELIST_VERSION
    authors: list[WhitelistedAuthor] = Field(default_factory=list)
    cursor: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "AuthorWhitelist":
        if not Path(path).is_file():
            return cls()
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls.model_validate(data)
        except Exception:
            logger.exception(
                "Malformed author whitelist at %s; treating as empty", path
            )
            return cls()

    def save(self, path: Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.model_dump(), fh, indent=2, ensure_ascii=False)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def add(self, author: WhitelistedAuthor) -> bool:
        for existing in self.authors:
            if existing.id == author.id:
                existing.name_aliases = sorted(
                    set(existing.name_aliases) | set(author.name_aliases)
                )
                existing.affiliation = author.affiliation or existing.affiliation
                existing.openalex_id = author.openalex_id or existing.openalex_id
                existing.orcid = author.orcid or existing.orcid
                existing.works_count = author.works_count or existing.works_count
                return False
        self.authors.append(author)
        return True

    def remove(self, token: str) -> WhitelistedAuthor | None:
        t = token.strip()
        tn = normalize_author_name(t)
        for i, a in enumerate(self.authors):
            if t and t in (a.id, a.orcid, a.openalex_id):
                return self.authors.pop(i)
            if normalize_author_name(a.display_name) == tn:
                return self.authors.pop(i)
        return None

    def _alias_to_author(self) -> dict[str, WhitelistedAuthor]:
        index: dict[str, WhitelistedAuthor] = {}
        for a in self.authors:
            for alias in a.normalized_aliases():
                index.setdefault(alias, a)
        return index

    def matches(self, article: ArticleInfo) -> WhitelistedAuthor | None:
        if not self.authors:
            return None
        index = self._alias_to_author()
        for name in split_author_names(article.authors or ""):
            hit = index.get(normalize_author_name(name))
            if hit is not None:
                return hit
        return None

    def matches_openalex_author_ids(
        self, ids: Iterable[str]
    ) -> WhitelistedAuthor | None:
        wanted = {i for i in ids if i}
        if not wanted:
            return None
        for a in self.authors:
            if a.openalex_id and a.openalex_id in wanted:
                return a
        return None

    def get_cursor(self, key: str) -> int:
        return int(self.cursor.get(key, 0))

    def set_cursor(self, key: str, message_id: int) -> None:
        if int(message_id) > self.get_cursor(key):
            self.cursor[key] = int(message_id)


def force_included_whitelist_items(recent_articles, replies, passing, whitelist):
    """Return (article, Reply, WhitelistedAuthor) tuples to ADD to ``passing``.

    A whitelist hit reuses the article's real score (so cross-group dedup can place
    it well) and tags the reason. Links already in ``passing`` are skipped.
    """
    from fastgpt_reply import Reply
    from rss_merge import normalize_link

    out = []
    if whitelist is None or not whitelist.authors:
        return out
    passing_links = {normalize_link(str(a.link)) for a, _ in passing}
    for article, reply in zip(recent_articles, replies):
        nl = normalize_link(str(article.link))
        if nl in passing_links:
            continue
        hit = whitelist.matches(article)
        if hit is None:
            continue
        out.append(
            (
                article,
                Reply(
                    relevance=reply.relevance,
                    impact=reply.impact,
                    reason=f"whitelisted author: {hit.display_name}",
                ),
                hit,
            )
        )
        passing_links.add(nl)
    return out
