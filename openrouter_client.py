"""OpenRouter chat-completions client (alternative to Kagi FastGPT / Summarizer)."""
from __future__ import annotations

import logging
import os
import random
import time
from typing import Any

import requests

from api_usage import record_openrouter_http

logger = logging.getLogger(__name__)

DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "~anthropic/claude-haiku-latest"

_SCORE_SYSTEM_PROMPT = (
    "You are an academic paper evaluator curating an RSS feed. "
    "Respond with ONLY a single JSON object with exactly these keys: "
    '"relevance" (integer 0-9), "impact" (integer 0-9), "reason" (string, short justification). '
    "No markdown, no code fences, no other text."
)

_SUMMARIZE_SYSTEM_PROMPT = (
    "You are a research assistant. Summarize the following discussion concisely, "
    "preserving key topics, decisions, and named entities. Output plain text only."
)

# Module-level usage counters (compatible with api_usage.py pattern).
_usage: dict[str, int] = {
    "calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
}


def reset_openrouter_usage() -> None:
    """Clear OpenRouter usage counters (call once per main() run)."""
    _usage["calls"] = 0
    _usage["input_tokens"] = 0
    _usage["output_tokens"] = 0


def get_openrouter_usage() -> dict[str, int]:
    """Return a snapshot of OpenRouter usage counters."""
    return dict(_usage)


def _record_usage(response: dict[str, Any]) -> None:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        _usage["calls"] += 1
        record_openrouter_http(1)
        return
    _usage["calls"] += 1
    _usage["input_tokens"] += int(usage.get("prompt_tokens") or 0)
    _usage["output_tokens"] += int(usage.get("completion_tokens") or 0)
    record_openrouter_http(1)


class OpenRouterClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        site_url: str = "",
        site_name: str = "llm-rss",
        api_url: str = DEFAULT_OPENROUTER_URL,
    ):
        self.api_key = (api_key or os.environ.get("OPENROUTER_API_KEY", "")).strip()
        self.model = (model or os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL).strip()
        self.timeout = float(timeout)
        self.max_retries = max(1, int(max_retries))
        self.site_url = site_url
        self.site_name = site_name
        self.api_url = api_url

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ValueError(
                "OpenRouter API key missing: set [openrouter] api_key or OPENROUTER_API_KEY"
            )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        if self.site_name:
            headers["X-Title"] = self.site_name
        return headers

    def _post_json_with_retries(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                r = requests.post(
                    self.api_url,
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout,
                )
            except requests.Timeout as e:
                last_exc = e
                if attempt < self.max_retries:
                    delay = min(60.0, (2 ** (attempt - 1))) + random.random()
                    logger.warning(
                        "OpenRouter request timed out; retrying in %.1fs (attempt %s/%s)",
                        delay,
                        attempt,
                        self.max_retries,
                    )
                    time.sleep(delay)
                    continue
                raise

            if r.status_code == 429:
                if attempt >= self.max_retries:
                    r.raise_for_status()
                retry_after = r.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        delay = max(0.0, float(retry_after))
                    except ValueError:
                        delay = 0.0
                else:
                    delay = min(60.0, (2 ** (attempt - 1))) + random.random()
                logger.warning(
                    "OpenRouter rate-limited (429); retrying in %.1fs (attempt %s/%s)",
                    delay,
                    attempt,
                    self.max_retries,
                )
                time.sleep(delay)
                continue

            try:
                r.raise_for_status()
            except requests.HTTPError as e:
                last_exc = e
                if 400 <= r.status_code < 500:
                    detail = (r.text or "").strip()
                    if detail:
                        logger.error(
                            "OpenRouter HTTP %s for model=%s: %s",
                            r.status_code,
                            payload.get("model"),
                            detail[:500],
                        )
                if 500 <= r.status_code < 600 and attempt < self.max_retries:
                    delay = min(60.0, (2 ** (attempt - 1))) + random.random()
                    logger.warning(
                        "OpenRouter server error (%s); retrying in %.1fs (attempt %s/%s)",
                        r.status_code,
                        delay,
                        attempt,
                        self.max_retries,
                    )
                    time.sleep(delay)
                    continue
                raise

            result = r.json()
            _record_usage(result)
            return result

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("OpenRouter request failed after retries")

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
        }
        result = self._post_json_with_retries(payload)
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("OpenRouter response missing choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ValueError("OpenRouter response missing message")
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("OpenRouter response missing text content")
        return content

    def summarize(self, text: str) -> str:
        return self.chat_completion(
            [
                {"role": "system", "content": _SUMMARIZE_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ]
        )

    def score_article(self, article_prompt: str) -> str:
        return self.chat_completion(
            [
                {"role": "system", "content": _SCORE_SYSTEM_PROMPT},
                {"role": "user", "content": article_prompt},
            ]
        )
