# OpenRouter Whole-Run Credit Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Abort each feed `main()` run before whitelist/RSS/scoring when remaining OpenRouter funds cannot cover a config-based whole-run cost estimate, and send `max_tokens` on completions so reservations match that estimate.

**Architecture:** New `openrouter_credits.py` computes planned calls, spendable USD (`GET /key`, optional `GET /credits`), and priced estimate; `OpenRouterClient` adds authenticated GETs plus `max_tokens` on chat completions; `main()` runs the preflight after `expand_groups` and before the whitelist bot.

**Tech Stack:** Python 3, `requests`, `unittest` (mocked HTTP; no live OpenRouter).

## Global Constraints

- Abort the whole `main()` pipeline for that config; no RSS or journal-suggestion writes.
- `--dispatch-feedback-queue` does not run this check.
- Skip preflight when OpenRouter client is missing or `route_to_openrouter` is empty.
- `GET /api/v1/key` is required; `GET /api/v1/credits` 401/403 is ignored; other credit errors abort.
- Cost = planned calls × (prompt×8000 + completion×max_tokens + request) × 1.25.
- Default `max_tokens` is 4096; completions always send it.
- Fail closed on key/models HTTP or pricing lookup failure.
- Dry-run aborts the same way.
- Preflight GET counts toward `OpenRouter_http`.
- No API key hash in log messages.

---

### Task 1: Planned call count

**Files:**
- Create: `openrouter_credits.py`
- Test: `tests/test_openrouter_credits.py`

**Interfaces:**
- Produces: `planned_openrouter_calls(groups, route_to_openrouter, *, default_cap: int = 20, default_batch: int = 5) -> int`

- [x] **Step 1: Write the failing test** for 2 groups, scoring only, cap 20 / batch 10 → 4 calls; plus summarize/curate/domains with `zulip_sources`.
- [x] **Step 2: Run test to verify it fails** (`python -m unittest tests.test_openrouter_credits`)
- [x] **Step 3: Implement `planned_openrouter_calls`**
- [x] **Step 4: Run tests and confirm they pass**

### Task 2: Spendable USD, pricing, ensure

**Files:**
- Modify: `openrouter_client.py` (authenticated GET helper)
- Modify: `openrouter_credits.py`
- Test: `tests/test_openrouter_credits.py`

**Interfaces:**
- Produces: `OpenRouterInsufficientCredits`, `ensure_openrouter_credits_for_run(...)`
- Consumes: `OpenRouterClient.get_json`, `planned_openrouter_calls`

- [ ] **Step 1: Failing tests** — abort below estimate; proceed above; `/credits` 403 uses key remaining; models lookup failure aborts; `credit_check=false` does not GET `/key`; unlimited key without account remaining aborts.
- [ ] **Step 2: Run tests (fail)**
- [ ] **Step 3: Implement fetch + estimate + ensure**
- [ ] **Step 4: Run tests (pass)**

### Task 3: Completions send `max_tokens`

**Files:**
- Modify: `openrouter_client.py`, `main.py` (`make_openrouter_client`)
- Test: `tests/test_openrouter_credits.py`

- [ ] **Step 1: Failing test** that chat payload includes `max_tokens` (default 4096)
- [ ] **Step 2: Run test (fail)**
- [ ] **Step 3: Add `max_tokens` to client + `chat_completion` payload**
- [ ] **Step 4: Run tests (pass)**

### Task 4: Wire `main()` + docs

**Files:**
- Modify: `main.py`, `config.d/config.toml.example`, `README.md`

- [ ] **Step 1: Failing test** that `credit_check=false` skips `/key` via `ensure_openrouter_credits_for_run`
- [ ] **Step 2: In `main()`, parse groups, call ensure before whitelist, log ERROR and return on `OpenRouterInsufficientCredits`**
- [ ] **Step 3: Document config keys**
- [ ] **Step 4: Run full unittest suite**
