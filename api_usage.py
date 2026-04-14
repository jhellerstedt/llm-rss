"""Track outbound HTTP/API usage for end-of-run logging."""

from __future__ import annotations

import logging
import threading
_lock = threading.Lock()
_kagi_fastgpt: int = 0
_kagi_summarize: int = 0
_openalex: int = 0
_zulip: int = 0
_rss_feed: int = 0
_rss_page: int = 0


def reset_api_usage_stats() -> None:
    """Clear counters (call once per config file / main() run)."""
    global _kagi_fastgpt, _kagi_summarize, _openalex, _zulip, _rss_feed, _rss_page
    with _lock:
        _kagi_fastgpt = 0
        _kagi_summarize = 0
        _openalex = 0
        _zulip = 0
        _rss_feed = 0
        _rss_page = 0


def record_kagi_fastgpt_http(n: int = 1) -> None:
    global _kagi_fastgpt
    if n <= 0:
        return
    with _lock:
        _kagi_fastgpt += n


def record_kagi_summarize_http(n: int = 1) -> None:
    global _kagi_summarize
    if n <= 0:
        return
    with _lock:
        _kagi_summarize += n


def record_openalex_http(n: int = 1) -> None:
    global _openalex
    if n <= 0:
        return
    with _lock:
        _openalex += n


def record_zulip_api(n: int = 1) -> None:
    global _zulip
    if n <= 0:
        return
    with _lock:
        _zulip += n


def record_rss_feed_fetch(n: int = 1) -> None:
    global _rss_feed
    if n <= 0:
        return
    with _lock:
        _rss_feed += n


def record_rss_page_fetch(n: int = 1) -> None:
    global _rss_page
    if n <= 0:
        return
    with _lock:
        _rss_page += n


def log_api_usage_summary(logger: logging.Logger | None = None) -> None:
    """Emit a single INFO line suitable for log files (e.g. update_llm_rss.sh tee)."""
    log = logger or logging.getLogger(__name__)
    with _lock:
        kf = _kagi_fastgpt
        ks = _kagi_summarize
        oa = _openalex
        zu = _zulip
        rf = _rss_feed
        rp = _rss_page
    total = kf + ks + oa + zu + rf + rp
    log.info(
        "API usage summary: total_calls=%s (Kagi_FastGPT_http=%s, Kagi_summarize_http=%s, "
        "OpenAlex_http=%s, Zulip_api=%s, RSS_feed_fetch=%s, RSS_abstract_page=%s)",
        total,
        kf,
        ks,
        oa,
        zu,
        rf,
        rp,
    )
