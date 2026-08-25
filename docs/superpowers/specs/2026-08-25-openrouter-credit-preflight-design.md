# OpenRouter whole-run credit preflight — design

Status: approved (2026-08-25 brainstorm)
Date: 2026-08-25

## Problem

The 2026-08-25 06:00 AEST feed run hit OpenRouter HTTP 402 after several
groups had already scored. The key’s **weekly limit** could afford 63,270
tokens, but each chat completion reserved **64,000** because we do not set
`max_tokens`. Batch scoring then fell back per article and repeated the same
402. RSS still wrote; the Zulip error digest was a flood of identical lines.

We need to **refuse to start** a feed run when remaining OpenRouter funds
cannot cover an **upper-bound estimate of the whole run**.

## Decisions (locked with user)

| Topic | Decision |
|-------|----------|
| When | After OpenRouter client is built, **before** whitelist bot, RSS, scoring, journal suggestions, or config writes |
| Abort | Entire `main()` pipeline for that config; no RSS / config writes |
| Hourly queue | `--dispatch-feedback-queue` does **not** run this check |
| Skip | OpenRouter client missing, or `route_to_openrouter` empty |
| Balance | `GET /api/v1/key` (`limit_remaining`) is required; `GET /api/v1/credits` is extra if the key allows it |
| Cost | Config upper bound × model pricing × **1.25** margin; not live RSS counts |
| Completions | Send `max_tokens` (default **4096**) so reservation matches the estimate |
| Dry-run | Same abort (so operators see the check) |
| API / pricing failure | **Fail closed** (abort) |
| 402 fallback | Out of scope (preflight should prevent the storm) |

## Architecture

| Unit | File | Responsibility |
|------|------|----------------|
| Preflight | `openrouter_credits.py` (new) | Fetch spendable USD; count planned calls; price the run; raise if short |
| Client | `openrouter_client.py` | Send `max_tokens` on chat completions; expose key/credits/models GETs used by preflight |
| Wire-up | `main.py` | Parse groups from TOML (no I/O), run preflight, **then** whitelist bot and group loop; catch abort; `finally` still posts Zulip digest |
| Config / docs | `config.d/config.toml.example`, `README.md` | Document `max_tokens` and `credit_check` |

### Data flow

```
main() loads TOML, builds OpenRouter client, parses groups from config (no RSS)
  └─ if client and route_to_openrouter non-empty:
        spendable = min(key.limit_remaining, account remaining if known)
        n_calls   = scoring batches + summarize + curate + domains (config upper bound)
        estimate  = n_calls × (prompt×8000 + completion×max_tokens + request) × 1.25
        if spendable is unknown or spendable < estimate:
          log one ERROR; return from main()
        else:
          log INFO spendable vs estimate; continue
  whitelist → RSS → scoring → journal suggestions → RSS write
finally: API summary + Zulip error digest (abort still reports the ERROR)
```

## Spendable USD

1. `GET https://openrouter.ai/api/v1/key` with the same Bearer token as completions.
   - `data.limit_remaining` number → key cap remaining (this is what 402’d).
   - `data.limit_remaining` null and `data.limit` null → no per-key cap.
   - HTTP error or missing `data` → abort.
2. `GET https://openrouter.ai/api/v1/credits`.
   - Success: account remaining = `data.total_credits - data.total_usage`.
   - 401/403 (typical for non-management keys): ignore; do not abort for this alone.
   - Other HTTP/parse errors: abort.
3. Spendable = minimum of the figures we actually have.
   - If the only figure is “unlimited key” and `/credits` was ignored, abort
     (cannot prove account funds). Unlimited key **plus** account remaining is OK.

Count these GETs toward `OpenRouter_http` usage.

## Whole-run call count (no RSS)

Use the same defaults as `main.py`: `[kagi] prefilter_max_candidates` (20),
`[kagi] scoring_batch_size` (5), per-group overrides, `[zulip] context_max_chars`.

| Routed type | Calls |
|-------------|--------|
| `scoring` | Per group: `ceil(cap / batch_size)`. If `cap <= 0`, use **50** as the article upper bound for that group |
| `summarize` | **1 per group that has `zulip_sources`** (assume context always needs compression) |
| `curate` | **1 per group that has `zulip_sources`** |
| `domains` | **1** if any group has `zulip_sources` |

Unrouted types contribute 0. Sum is `n_calls`. If `n_calls == 0`, skip the
money check (nothing to spend) but still send `max_tokens` on any later calls.

## Cost estimate

`GET https://openrouter.ai/api/v1/models` and find `id` equal to the configured
model (including `~anthropic/claude-haiku-latest`). If missing, try the id with
a leading `~` stripped. If still missing or `pricing.prompt` /
`pricing.completion` cannot be parsed: abort.

Prices are USD **per token**. Per call:

```
prompt_tokens      = 8000
completion_tokens  = max_tokens   # default 4096
cost_per_call      = prompt×prompt_tokens + completion×completion_tokens + request
estimate           = n_calls × cost_per_call × credit_check_margin
```

`request` is 0 if absent. `credit_check_margin` default **1.25**.

Abort if `spendable + 1e-9 < estimate`.

## Completions: `max_tokens`

`OpenRouterClient.chat_completion` always sends `max_tokens` (constructor
arg, default 4096, overridable in `[openrouter] max_tokens`). Preflight must
use the same value. This is required: OpenRouter 402s on the **reservation**,
not on actual output size.

## Config

```toml
[openrouter]
# max_tokens = 4096
# credit_check = true          # default true when OpenRouter is used
# credit_check_margin = 1.25
```

`credit_check = false` skips the preflight (emergency only). Completions still
send `max_tokens`.

## Abort behavior

- Raise `OpenRouterInsufficientCredits` (or equivalent) from the preflight
  helper; `main()` logs **one** ERROR and returns.
- Message includes spendable USD, estimate USD, and whether the binding
  constraint was key `limit_remaining` or account remaining. No API key hash.
- No RSS XML writes, no `config.toml` journal-suggestion rewrite.
- `RunLogCollector` + `maybe_post_run_error_summary` in `finally` still run,
  so Zulip gets a single “insufficient OpenRouter credits” line.

## Tests

`tests/test_openrouter_credits.py` (mocked HTTP):

- Abort when key `limit_remaining` is below estimate.
- Proceed when spendable is above estimate.
- `/credits` 403: still proceed using key remaining alone.
- Models lookup failure: abort.
- Call-count: 2 groups, scoring only, cap 20 / batch 10 → 4 scoring calls.
- `credit_check = false` does not GET `/key`.
- Chat payload includes `max_tokens`.

No live OpenRouter calls in CI.
