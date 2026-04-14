"""Batched FastGPT scoring for multiple articles in one Kagi call."""

from __future__ import annotations

import logging
from typing import Any

from adapter import ArticleInfo
from fastgpt_reply import parse_batch_replies_from_fastgpt_output

logger = logging.getLogger(__name__)


def _to_bullets(text_list: list[str]) -> str:
    return "\n".join(f"- {item}" for item in text_list)


def build_batch_scoring_query(
    batch_items: list[tuple[str, ArticleInfo, str]],
    group: dict[str, Any],
    zulip_block: str,
) -> str:
    """batch_items: (batch_id, article, feedback_snippet) per row."""
    research_areas = _to_bullets(list(group.get("research_areas") or []))
    excluded_areas = _to_bullets(list(group.get("excluded_areas") or []))
    zulip_section = ""
    if zulip_block.strip():
        zulip_section = (
            "\n### Context from Zulip (team discussion; may be summarized)\n"
            f"{zulip_block.strip()}\n"
        )
    lines: list[str] = []
    for bid, art, fb in batch_items:
        fb_stripped = (fb or "").strip()
        fb_block = f"\n{fb_stripped}\n" if fb_stripped else ""
        lines.append(
            f"#### {bid}\n{fb_block}title: {art.title}\nabstract: {art.abstract}\n"
        )
    articles_block = "\n".join(lines)
    ids_csv = ", ".join(bid for bid, _, _ in batch_items)
    return f"""You are an academic paper evaluator curating an RSS feed.
You will score MULTIPLE papers in one response. Each paper is labeled with an id like A1, A2, ...
Use the shared research context below for all papers.

User research areas:
{research_areas}

Excluded areas (generally lower relevance if the work is primarily in these):
{excluded_areas}
{zulip_section}
### Papers to score
{articles_block}
Respond with ONLY a single JSON object (no markdown code fences, no other text).
Keys MUST be exactly these ids: {ids_csv}
Each value MUST be an object with keys: "relevance" (integer 0-9), "impact" (integer 0-9), "reason" (string, optional).
Example for two papers: {{"A1": {{"relevance": 6, "impact": 5, "reason": "..."}}, "A2": {{"relevance": 4, "impact": 7}}}}
"""


def score_article_batch_with_kagi(
    kagi: Any,
    batch_items: list[tuple[str, ArticleInfo, str]],
    group: dict[str, Any],
    zulip_block: str,
) -> dict[str, Any]:
    """One fastgpt_query; returns mapping batch_id -> Reply."""
    if not batch_items:
        return {}
    query = build_batch_scoring_query(batch_items, group, zulip_block)
    output = kagi.fastgpt_query(query)
    expected = [bid for bid, _, _ in batch_items]
    return parse_batch_replies_from_fastgpt_output(output, expected)
