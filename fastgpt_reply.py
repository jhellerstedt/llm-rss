from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_INVALID_ESCAPE = re.compile(r"""\\(?!["\\/bfnrtu])""")
_BAD_UNICODE_ESCAPE = re.compile(r"""\\u(?![0-9a-fA-F]{4})""")


class Reply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    relevance: int
    impact: int
    reason: str | None = None


def extract_json_object(text: str) -> str:
    t = text.strip()
    t = _FENCE.sub("", t)
    t = re.sub(r"\s*```\s*$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        return t[start : end + 1]
    return t


def _sanitize_invalid_json_escapes(raw_json: str) -> str:
    # FastGPT sometimes returns LaTeX-like backslashes in a JSON string
    # (e.g. "\infty"), which is not valid JSON. We defensively escape any
    # backslash that isn't part of a valid JSON escape sequence.
    out = _BAD_UNICODE_ESCAPE.sub(r"\\\\u", raw_json)
    out = _INVALID_ESCAPE.sub(r"\\\\", out)
    return out


def try_load_json_object_from_llm(text: str) -> dict | None:
    """Parse a single JSON object from model output (handles fences and bad escapes)."""
    raw = extract_json_object(text)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        try:
            data = json.loads(_sanitize_invalid_json_escapes(raw))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


def parse_batch_replies_from_fastgpt_output(
    text: str, expected_ids: list[str]
) -> dict[str, Reply]:
    """Parse a JSON object mapping batch ids (e.g. A1, A2) to relevance/impact objects."""
    raw = extract_json_object(text)
    out: dict[str, Reply] = {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = json.loads(_sanitize_invalid_json_escapes(raw))
        except json.JSONDecodeError:
            logger.warning("Batch JSON decode failed; snippet=%s", text[:400])
            return out
    if not isinstance(data, dict):
        logger.warning("Batch scoring expected JSON object, got %s", type(data))
        return out
    for bid in expected_ids:
        val = data.get(bid)
        if not isinstance(val, dict):
            continue
        try:
            out[bid] = Reply.model_validate(val)
        except (ValueError, TypeError):
            logger.warning("Batch item %r invalid: %s", bid, val)
    return out


def parse_reply_from_fastgpt_output(text: str, article_title: str) -> Reply:
    raw = extract_json_object(text)
    try:
        data = json.loads(raw)
        return Reply.model_validate(data)
    except (json.JSONDecodeError, ValueError) as e:
        try:
            sanitized = _sanitize_invalid_json_escapes(raw)
            data = json.loads(sanitized)
            return Reply.model_validate(data)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "JSON decode failed for %r: %s; snippet=%s",
                article_title,
                e,
                text[:400],
            )
            return Reply(relevance=0, impact=0, reason="decode error")

