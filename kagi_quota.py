"""Per-main() limits on Kagi API usage (logical invocations, not HTTP retries)."""

from __future__ import annotations

import logging
import threading

_lock = threading.Lock()
# Each fastgpt_query or summarize() entry counts once toward the total (retries do not).
_kagi_invocations_used: int = 0
# Subset of fastgpt_query calls from OpenAlex metadata backfill only.
_openalex_fallback_fastgpt_used: int = 0

MAX_KAGI_INVOCATIONS_PER_RUN: int = 70
MAX_OPENALEX_FALLBACK_FASTGPT_PER_RUN: int = 5
# Worst-case slots to leave for post-scoring Kagi (OpenAlex metadata + margin).
DEFAULT_RESERVE_AFTER_SCORING: int = 7


class KagiSessionQuotaExceeded(RuntimeError):
    """Total Kagi fastgpt + summarize invocations for this run exceeded the cap."""


class KagiOpenAlexFallbackQuotaExceeded(RuntimeError):
    """OpenAlex → Kagi metadata backfill fastgpt calls exceeded the cap."""


def reset_kagi_session_quota() -> None:
    global _kagi_invocations_used, _openalex_fallback_fastgpt_used
    with _lock:
        _kagi_invocations_used = 0
        _openalex_fallback_fastgpt_used = 0


def consume_kagi_invocation(
    *,
    kind: str,
    openalex_fallback: bool = False,
) -> None:
    """Reserve one invocation slot; raises if limits would be exceeded.

    ``kind`` is ``\"fastgpt\"`` or ``\"summarize\"``.
    """
    global _kagi_invocations_used, _openalex_fallback_fastgpt_used
    if kind not in ("fastgpt", "summarize"):
        raise ValueError(f"unknown kind {kind!r}")
    with _lock:
        if _kagi_invocations_used >= MAX_KAGI_INVOCATIONS_PER_RUN:
            raise KagiSessionQuotaExceeded(
                f"Kagi session limit reached ({MAX_KAGI_INVOCATIONS_PER_RUN} invocations per run)"
            )
        if kind == "fastgpt" and openalex_fallback:
            if _openalex_fallback_fastgpt_used >= MAX_OPENALEX_FALLBACK_FASTGPT_PER_RUN:
                raise KagiOpenAlexFallbackQuotaExceeded(
                    "OpenAlex Kagi metadata fallback limit reached "
                    f"({MAX_OPENALEX_FALLBACK_FASTGPT_PER_RUN} fastgpt calls per run)"
                )
            _openalex_fallback_fastgpt_used += 1
        _kagi_invocations_used += 1


def remaining_kagi_invocations() -> int:
    """Fastgpt + summarize slots still available this run."""
    with _lock:
        return max(0, MAX_KAGI_INVOCATIONS_PER_RUN - _kagi_invocations_used)


def plan_scoring_budget(
    n_articles: int,
    *,
    prefilter_cap: int,
    batch_size: int,
    reserve_after_scoring: int = DEFAULT_RESERVE_AFTER_SCORING,
) -> tuple[int, int]:
    """Pick shortlist length and batch size so batch count fits remaining Kagi budget.

    Returns ``(shortlist_len, batch_size)``. ``shortlist_len`` may be 0 if no budget.
    """
    bs = max(1, int(batch_size))
    budget = remaining_kagi_invocations() - int(reserve_after_scoring)
    if n_articles <= 0 or budget <= 0:
        return 0, bs
    max_shortlist_by_budget = budget * bs
    shortlist = min(int(prefilter_cap), n_articles, max_shortlist_by_budget)
    batches_needed = (shortlist + bs - 1) // bs if shortlist else 0
    if batches_needed > budget:
        shortlist = max(0, budget * bs)
    return shortlist, bs


def log_kagi_quota_status(logger: logging.Logger | None = None) -> None:
    """Emit how many Kagi invocation slots were used (for log files)."""
    log = logger or logging.getLogger(__name__)
    with _lock:
        used = _kagi_invocations_used
        fb = _openalex_fallback_fastgpt_used
    log.info(
        "Kagi quota: invocations_used=%s/%s (fastgpt+summarize), "
        "openalex_metadata_fastgpt=%s/%s",
        used,
        MAX_KAGI_INVOCATIONS_PER_RUN,
        fb,
        MAX_OPENALEX_FALLBACK_FASTGPT_PER_RUN,
    )
