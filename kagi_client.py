"""Kagi FastGPT and Universal Summarizer API clients (Bot auth)."""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any

import requests

from api_usage import record_kagi_fastgpt_http, record_kagi_summarize_http

logger = logging.getLogger(__name__)

DEFAULT_FASTGPT_URL = "https://kagi.com/api/v0/fastgpt"
DEFAULT_SUMMARIZE_URL = "https://kagi.com/api/v0/summarize"


def _extract_output(result: dict[str, Any]) -> str | None:
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("output", "summary", "text", "content"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val
    for key in ("output", "text", "summary", "content"):
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return None


class KagiClient:
    def __init__(
        self,
        api_key: str | None = None,
        fastgpt_url: str = DEFAULT_FASTGPT_URL,
        summarize_url: str = DEFAULT_SUMMARIZE_URL,
        fastgpt_timeout: float = 120.0,
        summarize_timeout: float = 180.0,
        web_search: bool = True,
        summarize_engine: str = "muriel",
        use_cache: bool = True,
        max_concurrent_api_requests: int = 2,
        max_http_attempts: int = 12,
    ):
        self.api_key = (api_key or os.environ.get("KAGI_API_KEY", "")).strip()
        self.fastgpt_url = fastgpt_url
        self.summarize_url = summarize_url
        self.fastgpt_timeout = fastgpt_timeout
        self.summarize_timeout = summarize_timeout
        self.web_search = web_search
        self.summarize_engine = summarize_engine
        self.use_cache = use_cache
        mc = int(max_concurrent_api_requests) if max_concurrent_api_requests is not None else 2
        self.max_concurrent_api_requests = max(1, mc)
        self._api_semaphore = threading.BoundedSemaphore(self.max_concurrent_api_requests)
        self.max_http_attempts = max(1, int(max_http_attempts))

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ValueError("Kagi API key missing: set [kagi] api_key or KAGI_API_KEY")
        return {
            "Authorization": f"Bot {self.api_key}",
            "Content-Type": "application/json",
        }

    def _post_json_with_retries(
        self,
        url: str,
        payload: dict[str, Any],
        timeout: float,
        *,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        attempts = self.max_http_attempts if max_attempts is None else max_attempts
        self._api_semaphore.acquire()
        try:
            return self._post_json_with_retries_inner(url, payload, timeout, max_attempts=attempts)
        finally:
            self._api_semaphore.release()

    def _post_json_with_retries_inner(
        self,
        url: str,
        payload: dict[str, Any],
        timeout: float,
        *,
        max_attempts: int,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        rate_limit_attempts = 0
        for attempt in range(1, max_attempts + 1):
            r = requests.post(url, headers=self._headers(), json=payload, timeout=timeout)
            if url.rstrip("/") == self.fastgpt_url.rstrip("/"):
                record_kagi_fastgpt_http(1)
            elif url.rstrip("/") == self.summarize_url.rstrip("/"):
                record_kagi_summarize_http(1)
            if r.status_code == 429:
                rate_limit_attempts += 1
                if rate_limit_attempts >= max_attempts:
                    r.raise_for_status()
                retry_after = r.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        delay = max(0.0, float(retry_after))
                    except ValueError:
                        delay = 0.0
                else:
                    # Exponential backoff with a bit of jitter (cap avoids endless growth).
                    delay = min(120.0, (2 ** (attempt - 1))) + random.random()
                logger.warning(
                    "Kagi rate-limited (429); retrying in %.1fs (attempt %s/%s)",
                    delay,
                    rate_limit_attempts,
                    max_attempts,
                )
                time.sleep(delay)
                continue

            try:
                r.raise_for_status()
            except requests.HTTPError as e:
                last_exc = e
                # Retry transient 5xx.
                if 500 <= r.status_code < 600 and attempt < max_attempts:
                    delay = min(120.0, (2 ** (attempt - 1))) + random.random()
                    logger.warning(
                        "Kagi server error (%s); retrying in %.1fs (attempt %s/%s)",
                        r.status_code,
                        delay,
                        attempt,
                        max_attempts,
                    )
                    time.sleep(delay)
                    continue
                raise

            return r.json()

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Kagi request failed after retries")

    def fastgpt_query(self, query: str) -> str:
        payload: dict[str, Any] = {
            "query": query,
            "web_search": self.web_search,
            "cache": self.use_cache,
        }
        result = self._post_json_with_retries(
            self.fastgpt_url,
            payload,
            self.fastgpt_timeout,
        )
        out = _extract_output(result)
        if out is None:
            logger.warning("Unexpected FastGPT JSON keys: %s", list(result.keys()) if isinstance(result, dict) else type(result))
            raise ValueError("FastGPT response missing text output")
        return out

    def summarize(self, text: str, summary_type: str = "summary") -> str:
        payload: dict[str, Any] = {
            "text": text,
            "engine": self.summarize_engine,
        }
        if summary_type and summary_type != "summary":
            payload["summary_type"] = summary_type
        result = self._post_json_with_retries(
            self.summarize_url,
            payload,
            self.summarize_timeout,
        )
        out = _extract_output(result)
        if out is None:
            logger.warning("Unexpected Summarizer JSON keys: %s", list(result.keys()) if isinstance(result, dict) else type(result))
            raise ValueError("Summarizer response missing text output")
        return out
