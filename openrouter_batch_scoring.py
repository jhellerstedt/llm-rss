"""Batched OpenRouter scoring for multiple articles in one call."""

from __future__ import annotations

import logging
from typing import Any

from adapter import ArticleInfo
from fastgpt_reply import parse_batch_replies_from_fastgpt_output
from kagi_batch_scoring import build_batch_scoring_query

logger = logging.getLogger(__name__)


def score_article_batch_with_openrouter(
    openrouter: Any,
    batch_items: list[tuple[str, ArticleInfo, str]],
    group: dict[str, Any],
    zulip_block: str,
) -> dict[str, Any]:
    """One chat_completion; returns mapping batch_id -> Reply."""
    if not batch_items:
        return {}
    query = build_batch_scoring_query(batch_items, group, zulip_block)
    output = openrouter.chat_completion([{"role": "user", "content": query}])
    expected = [bid for bid, _, _ in batch_items]
    return parse_batch_replies_from_fastgpt_output(output, expected)
