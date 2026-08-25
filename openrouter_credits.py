"""Upper-bound OpenRouter call counts and whole-run credit preflight."""
from __future__ import annotations

import logging
import math
from typing import Any, Sequence

import requests

logger = logging.getLogger(__name__)

PROMPT_TOKENS_PER_CALL = 8000
DEFAULT_MAX_TOKENS = 4096
DEFAULT_CREDIT_CHECK_MARGIN = 1.25
DEFAULT_UNCAP_ARTICLES = 50


class OpenRouterInsufficientCredits(RuntimeError):
    """Preflight determined the run cannot be funded, or balance APIs failed closed."""


def planned_openrouter_calls(
    groups: Sequence[dict[str, Any]],
    route_to_openrouter: Sequence[str],
    *,
    default_cap: int = 20,
    default_batch: int = 5,
) -> int:
    """Config-only upper bound of OpenRouter HTTP chat calls for a feed run."""
    routed = set(route_to_openrouter or [])
    n = 0
    any_zulip = False
    for group in groups:
        has_zulip = bool(group.get("zulip_sources"))
        if has_zulip:
            any_zulip = True
        if "scoring" in routed:
            cap_raw = group.get("prefilter_max_candidates")
            cap = default_cap if cap_raw is None else int(cap_raw)
            if cap <= 0:
                cap = DEFAULT_UNCAP_ARTICLES
            bsz_raw = group.get("scoring_batch_size")
            bsz = default_batch if bsz_raw is None else int(bsz_raw)
            bsz = max(1, bsz)
            n += math.ceil(cap / bsz)
        if has_zulip and "summarize" in routed:
            n += 1
        if has_zulip and "curate" in routed:
            n += 1
    if any_zulip and "domains" in routed:
        n += 1
    return n


def _http_status(exc: BaseException) -> int | None:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        try:
            return int(exc.response.status_code)
        except (TypeError, ValueError):
            return None
    return None


def fetch_spendable_usd(client: Any) -> tuple[float, str]:
    """Return (spendable_usd, constraint_label). Fail closed on key/credit errors."""
    try:
        key_doc = client.get_json("/key")
    except Exception as e:
        raise OpenRouterInsufficientCredits(f"OpenRouter GET /key failed: {e}") from e
    data = key_doc.get("data") if isinstance(key_doc, dict) else None
    if not isinstance(data, dict):
        raise OpenRouterInsufficientCredits("OpenRouter GET /key missing data")

    key_remaining: float | None = None
    lr = data.get("limit_remaining")
    lim = data.get("limit")
    if lr is None and lim is None:
        key_remaining = None
    elif lr is None:
        raise OpenRouterInsufficientCredits("OpenRouter GET /key missing limit_remaining")
    else:
        try:
            key_remaining = float(lr)
        except (TypeError, ValueError) as e:
            raise OpenRouterInsufficientCredits(
                f"OpenRouter GET /key limit_remaining unusable: {e}"
            ) from e

    account_remaining: float | None = None
    try:
        credits_doc = client.get_json("/credits")
    except requests.HTTPError as e:
        st = _http_status(e)
        if st not in (401, 403):
            raise OpenRouterInsufficientCredits(f"OpenRouter GET /credits failed: {e}") from e
        credits_doc = None
    except Exception as e:
        raise OpenRouterInsufficientCredits(f"OpenRouter GET /credits failed: {e}") from e
    else:
        cdata = credits_doc.get("data") if isinstance(credits_doc, dict) else None
        if not isinstance(cdata, dict):
            raise OpenRouterInsufficientCredits("OpenRouter GET /credits missing data")
        try:
            account_remaining = float(cdata["total_credits"]) - float(cdata["total_usage"])
        except (KeyError, TypeError, ValueError) as e:
            raise OpenRouterInsufficientCredits(
                f"OpenRouter GET /credits parse failed: {e}"
            ) from e

    figures: list[tuple[float, str]] = []
    if key_remaining is not None:
        figures.append((key_remaining, "key limit_remaining"))
    if account_remaining is not None:
        figures.append((account_remaining, "account remaining"))
    if not figures:
        raise OpenRouterInsufficientCredits(
            "OpenRouter funds unknown: unlimited key and no account remaining "
            "(GET /credits not available)"
        )
    return min(figures, key=lambda item: item[0])


def lookup_model_pricing(client: Any, model: str) -> tuple[float, float, float]:
    """Return (prompt, completion, request) USD per token / per request."""
    try:
        doc = client.get_json("/models")
    except Exception as e:
        raise OpenRouterInsufficientCredits(f"OpenRouter GET /models failed: {e}") from e
    models = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(models, list):
        raise OpenRouterInsufficientCredits("OpenRouter GET /models missing data")
    by_id = {m.get("id"): m for m in models if isinstance(m, dict)}
    row = by_id.get(model)
    if row is None and model.startswith("~"):
        row = by_id.get(model[1:])
    if row is None and not model.startswith("~"):
        row = by_id.get("~" + model)
    if row is None:
        raise OpenRouterInsufficientCredits(f"OpenRouter model pricing not found for {model}")
    pricing = row.get("pricing") or {}
    if not isinstance(pricing, dict):
        raise OpenRouterInsufficientCredits(f"OpenRouter model pricing unusable for {model}")
    try:
        prompt = float(pricing["prompt"])
        completion = float(pricing["completion"])
    except (KeyError, TypeError, ValueError) as e:
        raise OpenRouterInsufficientCredits(
            f"OpenRouter model pricing unusable for {model}: {e}"
        ) from e
    try:
        request = float(pricing.get("request") or 0)
    except (TypeError, ValueError):
        request = 0.0
    return prompt, completion, request


def estimate_run_cost_usd(
    n_calls: int,
    prompt_price: float,
    completion_price: float,
    request_price: float,
    max_tokens: int,
    margin: float,
) -> float:
    per_call = (
        prompt_price * PROMPT_TOKENS_PER_CALL
        + completion_price * max_tokens
        + request_price
    )
    return n_calls * per_call * margin


def ensure_openrouter_credits_for_run(
    client: Any,
    groups: Sequence[dict[str, Any]],
    route_to_openrouter: Sequence[str],
    *,
    openrouter_table: dict[str, Any] | None = None,
    kagi_table: dict[str, Any] | None = None,
) -> None:
    """Abort with OpenRouterInsufficientCredits if remaining funds cannot cover the run."""
    table = openrouter_table or {}
    if not bool(table.get("credit_check", True)):
        return
    if not route_to_openrouter:
        return
    kagi = kagi_table or {}
    default_cap = int(kagi.get("prefilter_max_candidates", 20))
    default_batch = int(kagi.get("scoring_batch_size", 5))
    n_calls = planned_openrouter_calls(
        groups,
        route_to_openrouter,
        default_cap=default_cap,
        default_batch=default_batch,
    )
    if n_calls <= 0:
        return

    max_tokens = int(getattr(client, "max_tokens", 0) or table.get("max_tokens") or DEFAULT_MAX_TOKENS)
    max_tokens = max(1, max_tokens)
    margin = float(table.get("credit_check_margin", DEFAULT_CREDIT_CHECK_MARGIN))
    spendable, source = fetch_spendable_usd(client)
    model = str(getattr(client, "model", "") or table.get("model") or "")
    prompt_p, completion_p, request_p = lookup_model_pricing(client, model)
    estimate = estimate_run_cost_usd(
        n_calls, prompt_p, completion_p, request_p, max_tokens, margin
    )
    if spendable + 1e-9 < estimate:
        raise OpenRouterInsufficientCredits(
            f"OpenRouter insufficient credits for whole run: spendable ${spendable:.4f} "
            f"({source}) < estimate ${estimate:.4f} ({n_calls} calls, max_tokens={max_tokens})"
        )
    logger.info(
        "OpenRouter credit check ok: spendable $%.4f (%s) >= estimate $%.4f "
        "(%s calls, max_tokens=%s)",
        spendable,
        source,
        estimate,
        n_calls,
        max_tokens,
    )
