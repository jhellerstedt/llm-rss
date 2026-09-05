# OpenRouter model selection

Status: keep explicit Haiku (do not switch to Auto Router)
Date: 2026-09-05

Investigation of how this repo uses OpenRouter chat completions, and whether
it is worth switching `model` to OpenRouter Auto Router (`openrouter/auto`).

**Decision:** stay on `~anthropic/claude-haiku-latest` (or another explicit
Haiku slug). Auto Router is a poor fit for this pipeline.

## Current use

One `OpenRouterClient` (`openrouter_client.py`) with a **single** `model` for
every routed call. The slug comes from `[openrouter] model`, else
`OPENROUTER_MODEL`, else the client default `~anthropic/claude-haiku-latest`
(OpenRouter alias that tracks the current Haiku family; as of this date that
is Haiku 4.5).

`route_to_openrouter` chooses which LLM call types leave Kagi. Kagi remains
for web-search-dependent work (for example OpenAlex metadata backfill).

| Kind | Code | When | Typical volume | Output contract |
|------|------|------|----------------|-----------------|
| `scoring` | `openrouter_batch_scoring.py` (fallback: `get_openrouter_reply` in `main.py`) | After keyword shortlist; batches of `scoring_batch_size` (example: 10), cap `prefilter_max_candidates` (example: 20) | Most of every feed run (~1–2 chat calls per group) | Strict JSON object keyed `A1`…`An` with `relevance` / `impact` integers 0–9 |
| `summarize` | `OpenRouterClient.summarize` via `zulip_context.py` | Once per group if Zulip context exceeds `context_max_chars` | Low | Plain-text digest |
| `curate` | `curate_group_research_lists_with_openrouter` | End of run, per group with `zulip_sources` | At most one call per such group | JSON rewrite of `research_areas` / `excluded_areas` |
| `domains` | `filter_academic_journal_domains_with_openrouter` | Once per run if journal suggestions exist | One call | JSON `academic_domains` allowlist |

Completions always send `max_tokens` (default **4096**) so OpenRouter’s
reservation matches the credit preflight. Before scoring,
`ensure_openrouter_credits_for_run` (`openrouter_credits.py`) does
`GET /key`, optional `GET /credits`, and `GET /models` to price the
**configured** slug. Those GETs count in `OpenRouter_http` but not in the
chat `OpenRouter usage: calls=` line.

`--dispatch-feedback-queue` does not run the credit check or scoring.

## Production logs (token usage)

Nine feed runs in `logs/update_llm_rss.log` emitted `OpenRouter usage:`
(chat completions only). Early runs logged
`model=anthropic/claude-3-5-haiku`; later runs
`model=~anthropic/claude-haiku-latest`. Example config routes all four kinds
above. A typical run scores ~18 groups.

| Run | Model in log | Calls | Input tokens | Output tokens |
|-----|--------------|------:|-------------:|--------------:|
| 1 | 3.5 Haiku | 52 | 228343 | 17944 |
| 2 | 3.5 Haiku | 56 | 89711 | 12054 |
| 3 | 3.5 Haiku | 37 | 78535 | 13283 |
| 4 | 3.5 Haiku | 80 | 168646 | 17654 |
| 5 | Haiku latest | 50 | 203521 | 24155 |
| 6 | Haiku latest | 52 | 204061 | 26979 |
| 7 | Haiku latest | 49 | 158699 | 20929 |
| 8 | Haiku latest | 40 | 90454 | 18542 |
| 9 | Haiku latest | 38 | 73602 | 21558 |

Mean ≈ **50 calls**, **144k** input / **19k** output tokens per run. Call
count jumps when batch JSON fails and single-article fallbacks fire (run 4).
Later runs are cheaper mainly because shortlists shrank, not because the
model changed.

**Estimated spend** at OpenRouter Haiku 4.5 list prices on 2026-09-05
($1 / $5 per million input/output tokens), applied to those token counts:
about **$0.18–$0.34 per run** (mean ~$0.24). Early 3.5 Haiku runs were
probably cheaper at the time; the range above prices every run at current
Haiku 4.5 so later runs are comparable.

## What Auto Router does

Sending `model: "openrouter/auto"` classifies each prompt into ~30 task
types, then picks from a trailing 7-day community **spend-share** ranking,
filtered by `cost_tier` (`low` default, then `medium`, `high`, `xhigh`,
`max`). You pay the selected model’s rate; the router itself has no
surcharge.

OpenRouter’s docs: use Auto when you do **not** know the next prompt; set a
model when you do. See
[Auto Router](https://openrouter.ai/docs/guides/routing/routers/auto-router).

On 2026-09-05, `GET /api/v1/models` listed Auto pricing as
`prompt=-1`, `completion=-1` (same for `openrouter/auto-beta`).
`~anthropic/claude-haiku-latest` listed `$1 / $5` per million tokens.

## Why not switch

| Criterion | This pipeline | Auto Router |
|-----------|---------------|-------------|
| Prompt mix | Four fixed templates; scoring dominates | Optimised for unknown, mixed user prompts |
| Output shape | Must parse as JSON; failures already appear in logs | Model can change per request; instruction-following varies |
| Cost control | Pinned cheap model; ~$0.24/run | Billed at whoever wins; `low` is a **band** (not a ceiling); “research-like” scoring could still land on a heavier model |
| Credit preflight | Prices the configured slug; abort if short | Auto has no real list price (`-1`); `lookup_model_pricing` would produce a negative estimate and always pass |
| Staying current | Already solved with `~anthropic/claude-haiku-latest` | Follows 7-day market spend, not this app’s JSON reliability |

Scoring is high-volume, low-creativity extraction (titles, abstracts,
research-area bullets → integers). A slightly smarter 0–9 score is not worth
a runaway bill or markdown-wrapped JSON that fails
`parse_batch_replies_from_fastgpt_output`.

Curation is the one call where a stronger model might help
(`research_areas` rewrites already fail to parse in logs). That is a
per-group, end-of-run call — not a reason to put Auto on the scoring hot
path.

**Do not** set `model = "openrouter/auto"` without changing
`lookup_model_pricing`. Auto’s listed prices would make the whole-run
credit check meaningless.

## Alternatives if routing is still wanted

Prefer controls that keep the model choice explicit:

1. **Keep `~anthropic/claude-haiku-latest`** — current setup; latest cheap
   Haiku, stable JSON, priced preflight.
2. **`models` fallback array** — you rank Haiku then a backup; OpenRouter
   walks the list on error. Not Auto. The client does not send this today.
3. **Per-kind models** — Haiku for scoring, a stronger slug only for
   `curate`. Low volume; would need a client/config change.

`provider.max_price` can still cap endpoint price for a **known** model.
That is separate from Auto Router.
