"""Kagi FastGPT and Universal Summarizer API clients (Bot auth)."""
from __future__ import annotations

import logging
import os
from typing import Any

import requests

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
    ):
        self.api_key = (api_key or os.environ.get("KAGI_API_KEY", "")).strip()
        self.fastgpt_url = fastgpt_url
        self.summarize_url = summarize_url
        self.fastgpt_timeout = fastgpt_timeout
        self.summarize_timeout = summarize_timeout
        self.web_search = web_search
        self.summarize_engine = summarize_engine
        self.use_cache = use_cache

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ValueError("Kagi API key missing: set [kagi] api_key or KAGI_API_KEY")
        return {
            "Authorization": f"Bot {self.api_key}",
            "Content-Type": "application/json",
        }

    def fastgpt_query(self, query: str) -> str:
        payload: dict[str, Any] = {
            "query": query,
            "web_search": self.web_search,
            "cache": self.use_cache,
        }
        r = requests.post(
            self.fastgpt_url,
            headers=self._headers(),
            json=payload,
            timeout=self.fastgpt_timeout,
        )
        r.raise_for_status()
        result = r.json()
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
        r = requests.post(
            self.summarize_url,
            headers=self._headers(),
            json=payload,
            timeout=self.summarize_timeout,
        )
        r.raise_for_status()
        result = r.json()
        out = _extract_output(result)
        if out is None:
            logger.warning("Unexpected Summarizer JSON keys: %s", list(result.keys()) if isinstance(result, dict) else type(result))
            raise ValueError("Summarizer response missing text output")
        return out
