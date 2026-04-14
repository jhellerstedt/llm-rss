"""Deterministic local ranking to shortlist articles before Kagi scoring."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from adapter import ArticleInfo
from rss_merge import normalize_link

_TOKEN = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def _bag(text: str) -> Counter[str]:
    return Counter(m.group(0).lower() for m in _TOKEN.finditer(text or "") if len(m.group(0)) > 2)


def _area_tokens(areas: list[str]) -> Counter[str]:
    c: Counter[str] = Counter()
    for line in areas:
        c.update(_bag(line))
    return c


def local_article_score(
    article: ArticleInfo,
    group: dict[str, Any],
    feedback_signals: dict[str, tuple[int, int]] | None = None,
    *,
    exclude_weight: float = 2.0,
    feedback_weight: float = 0.35,
) -> float:
    """Higher is more likely to be relevant; used only for shortlist ordering."""
    text = f"{article.title}\n{article.abstract}"
    doc = _bag(text)
    if not doc:
        return 0.0

    pos = _area_tokens(list(group.get("research_areas") or []))
    neg = _area_tokens(list(group.get("excluded_areas") or []))

    score = 0.0
    for w, n in pos.items():
        if w in doc:
            score += n * doc[w] * 1.0
    for w, n in neg.items():
        if w in doc:
            score -= exclude_weight * n * doc[w]

    title_bag = _bag(article.title)
    for w, n in pos.items():
        if w in title_bag:
            score += 0.5 * n * title_bag[w]

    if feedback_signals:
        key = normalize_link(str(article.link))
        if key in feedback_signals:
            up, down = feedback_signals[key]
            score += feedback_weight * (up - down)

    return score


def shortlist_for_kagi_scoring(
    articles: list[ArticleInfo],
    group: dict[str, Any],
    max_candidates: int,
    feedback_signals: dict[str, tuple[int, int]] | None = None,
) -> list[ArticleInfo]:
    """Return up to ``max_candidates`` articles with highest local scores (stable tie-break by title)."""
    if max_candidates <= 0:
        return []
    scored = [(local_article_score(a, group, feedback_signals), a.title, i, a) for i, a in enumerate(articles)]
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    out: list[ArticleInfo] = []
    for _, _, _, art in scored[:max_candidates]:
        out.append(art)
    return out
