# Author whitelist + bot-managed authors — design

Status: proposed (awaiting review)
Date: 2026-06-15

## Problem

The pipeline filters articles purely by LLM relevance/impact scores. Papers from
authors we specifically care about can score below threshold and silently drop
out (e.g. the same-day Nature article `s41586-026-10636-y`, which is fetched but
scored below the group's thresholds).

We want two capabilities:

1. **Author whitelist** — if any author of a fetched article is on the
   whitelist, the article is **always included** in the RSS feed output (and
   eligible for Zulip feedback posts), bypassing the relevance/impact thresholds.
2. **Bot-managed whitelist** — add authors by posting a command in a dedicated
   Zulip topic, passing either an **ORCID id/URL** or a **Google Scholar profile
   URL**. The bot resolves the author's identity, stores it, and replies with what
   it resolved.

## Decisions (locked with user)

- **Bot mechanism:** the existing cron run polls a dedicated Zulip topic each
  cycle for command messages; no new always-on daemon.
- **Matching:** resolve full identity via OpenAlex/ORCID **at add time** (capture
  ORCID, OpenAlex author ID, and all known name aliases). At article-scan time,
  match by **normalized author name** against the stored alias set (immediate, no
  OpenAlex indexing lag), and **also** by OpenAlex author ID when an article is
  already resolved. This deliberately avoids strict scan-time OpenAlex resolution,
  whose indexing lag would miss same-day articles — the exact case we care about.
- **Storage:** a JSON sidecar file, matching the project's file-based state
  pattern. No SQLite.
- **Flag behavior:** bypass the LLM thresholds entirely → always included in RSS
  output and eligible for Zulip feedback.
- **Scope:** global whitelist (an article still only appears in groups whose
  feeds carry it).
- **Resolution UX:** auto-pick the best OpenAlex match, add immediately, reply
  in-topic with the resolved identity for human verification; provide a `remove`
  command to undo.

## Architecture overview

Three new modules plus small edits to `main.py`:

| Unit | File | Responsibility |
|------|------|----------------|
| Store | `author_whitelist.py` | Load/save the JSON whitelist; expose a fast `matches(article) -> WhitelistedAuthor \| None` over normalized names + OpenAlex IDs. |
| Resolver | `author_resolve.py` | Turn an ORCID id/URL or Scholar profile URL into a canonical `WhitelistedAuthor` (name, aliases, ORCID, OpenAlex id, affiliation, works count) via ORCID public API + OpenAlex. |
| Bot commands | `author_whitelist_bot.py` | Poll the configured Zulip topic, parse `add` / `remove` / `list` commands, call the resolver + store, and reply in-topic. Tracks a processed-message cursor to avoid reprocessing. |
| Pipeline hook | `main.py` | (a) run the bot poll once per run; (b) in `process_group`, force whitelist-matched articles into `passing`. |

### Data flow

```
cron run
  └─ run_author_whitelist_bot()            # once per run, before groups
        ├─ fetch new messages in topic "author whitelist"
        ├─ for each "add <url>": resolve → store.add() → reply
        ├─ for each "remove <id>": store.remove() → reply
        └─ persist processed-message cursor

  └─ for each group: process_group()
        ├─ fetch recent_articles (unchanged)
        ├─ score shortlist (unchanged)
        ├─ compute passing by threshold (unchanged)
        ├─ NEW: whitelist_hits = [a for a in recent_articles
        │                         if whitelist.matches(a)]
        │        merge whitelist_hits into passing (dedup by link),
        │        using existing reply if scored else a synthetic
        │        Reply(reason="whitelisted author: <name>")
        └─ enrich + build feed items (unchanged)
```

## Storage format

`config.d/author_whitelist.json` (path configurable via `[author_whitelist] file`):

```json
{
  "version": 1,
  "authors": [
    {
      "id": "https://orcid.org/0000-0002-1825-0097",
      "display_name": "Josiah Carberry",
      "name_aliases": ["josiah carberry", "j. carberry", "josiah s. carberry"],
      "orcid": "0000-0002-1825-0097",
      "openalex_id": "A5023888391",
      "affiliation": "Brown University",
      "works_count": 142,
      "source": "orcid",
      "added_by": "alice@example.com",
      "added_at": "2026-06-15T02:00:00Z"
    }
  ],
  "cursor": { "realm:stream:author whitelist": 408123 }
}
```

- `id` is the stable dedup key: ORCID URL if known, else `openalex:<id>`, else
  `name:<normalized>`.
- `name_aliases` are pre-normalized (lowercased, whitespace-collapsed, using the
  existing `_norm_person_name` rules) and include the OpenAlex
  `display_name_alternatives`. This alias set is what scan-time matching uses.
- `cursor` records the highest Zulip message id processed per realm/stream/topic
  so commands are handled exactly once across runs.

## Matching (scan time)

`AuthorWhitelist.matches(article)`:
1. Split `article.authors` (the RSS `dc:creator` string, comma-joined) into names,
   normalize each.
2. Return the first whitelisted author whose `name_aliases` intersects the
   article's normalized names.
3. (If an article already has OpenAlex enrichment with author IDs available, also
   match on `openalex_id`. In the current pipeline enrichment happens after
   threshold, so the name path is the primary one; the ID path is a cheap extra
   when present and future-proofs against enrichment moving earlier.)

No network calls at scan time. O(articles × whitelist) with small sets; we build a
single `set` of all aliases for an O(1) gate.

## Resolver (add time)

`author_resolve.resolve(input_str) -> WhitelistedAuthor`:

- **ORCID** (`0000-0002-1825-0097` or `https://orcid.org/...`):
  1. `GET https://pub.orcid.org/v3.0/<orcid>/person` (Accept: application/json) →
     given/family name + `other-names`.
  2. `GET https://api.openalex.org/authors/orcid:<orcid>` → OpenAlex id,
     `display_name`, `display_name_alternatives`, `last_known_institutions`,
     `works_count`. Reuses `OPENALEX_BASE`, `mailto`, and `record_openalex_http`.
  3. Merge names from both into `name_aliases`.
- **Google Scholar** (`https://scholar.google.com/citations?user=...`):
  1. Best-effort scrape of the profile page for display name + affiliation
     (`#gsc_prf_in`, `.gsc_prf_il`). Scholar may rate-limit/captcha — on failure,
     reply asking for an ORCID instead.
  2. `GET https://api.openalex.org/authors?search=<name>` (optionally filtered by
     affiliation), pick the top-ranked match → OpenAlex id + ORCID + aliases.
- Unrecognized input → resolver raises; bot replies with usage help.

Best-effort: ambiguous Scholar/name lookups auto-pick OpenAlex's top hit and the
reply shows enough identity (name, affiliation, ORCID, OpenAlex id, works count)
for a human to catch a wrong match and `remove` it.

## Bot commands (topic, e.g. `author whitelist`)

Grammar (case-insensitive leading keyword; bot ignores its own messages):

- `add <orcid-or-scholar-url>` → resolve, store, reply with resolved identity.
- `remove <orcid | openalex-id | name>` → delete matching entry, reply.
- `list` → reply with current whitelist (name + affiliation + id).

Reply examples:

```
✅ Added Josiah Carberry (Brown University)
   ORCID 0000-0002-1825-0097 · OpenAlex A5023888391 · 142 works
   Their papers will now always be included regardless of score.
```
```
⚠️ Couldn't resolve that. Send an ORCID id/URL, or a Google Scholar profile URL.
```

Idempotency: only messages with id greater than the stored `cursor` for that
realm/stream/topic are processed; the cursor advances after each run.

## Config

The deployment uses a single `config.d/config.toml` with ~19 `[[groups]]` across
realms `tuesday` / `browave` / `saa`. The whitelist is global, so it is one new
**top-level** section (sibling to `[kagi]`, `[zulip]`, `[openalex]`), not
per-group:

```toml
[author_whitelist]
# enabled = true                     # default: true when this section is present
# file = "author_whitelist.json"     # relative to this TOML's dir (config.d/)
# Bot command source — reuses zulip_realms.json credentials. Pick a realm/stream
# the team can post to; the topic is dedicated to whitelist commands.
command_source = { realm = "tuesday", stream = "science", topic = "author whitelist", lookback_hours = 168, max_messages = 200 }
```

Notes:
- `config.d/**/*.json` is gitignored, so `config.d/author_whitelist.json` is
  local state (never committed), like the existing
  `config.journal_weekly_summary_state.json`.
- If `[author_whitelist]` is absent, behavior is unchanged (no whitelist, no bot
  poll).
- The bot poll runs once per run (not per group). If `main.py` is ever pointed at
  multiple config files, each file polls its own `command_source` and reads/writes
  its own `file`; share a whitelist by pointing `file` at the same path (atomic
  write + per-topic cursor keep this safe).

## Integration points in `main.py`

- `main()` (~after `zulip_realms` load, before the group loop): if a command
  source is configured and `zulip_realms` present, call
  `run_author_whitelist_bot(...)` (skipped on `--dryrun` for the *write/reply*
  side; still parses and logs).
- `process_group()` (right after `passing` is computed, ~line 597): load the
  whitelist once, compute `whitelist_hits`, and merge into `passing` (dedup by
  normalized link). Whitelisted-but-unscored articles get
  `Reply(relevance=<score or 0>, impact=<score or 0>, reason="whitelisted author: <name>")`.
  Whitelisted articles are then enriched and emitted by the existing code path,
  and (being in `passing` → `link_scores`) survive cross-group dedup with no
  extra mechanism.

### Cross-group dedup interaction

`winning_group_by_link` keeps each link in exactly one group — the one where it
appears in `link_scores` with the highest `(relevance, impact)`. Because whitelist
hits are merged into that group's `passing` (and therefore into `link_scores`),
they automatically survive dedup and land in their winning group's feed; no extra
"force-keep" mechanism is required. A whitelist hit reuses its real `Reply` when
the article was actually scored (better group placement), otherwise a synthetic
`Reply(relevance=0, impact=0, reason="whitelisted author: <name>")`. When the same
whitelisted paper is force-added in several groups, all carry the same synthetic
score and the existing relevance/impact/name tie-break picks one deterministically.

## Error handling

- ORCID/OpenAlex/Scholar HTTP failures: caught per-command; bot replies with a
  friendly error and **does** advance the cursor past the failed message (avoids
  infinite retry/spam loops) while logging the failure. The user can re-issue the
  command.
- Malformed whitelist JSON: log + treat as empty (don't crash the run); never
  overwrite a file that failed to parse unless a successful add/remove rewrites it.
- Scholar scrape blocked: reply asking for ORCID; no crash.
- Whitelist file write is atomic (write temp + rename), like other state writers.

## Testing

No live network in tests (repo has no live-network tests). Plan:

- `author_whitelist.py`: round-trip load/save; `matches()` hits on alias, case,
  and whitespace variants; misses on non-members; OpenAlex-id match path.
- `author_resolve.py`: parse ORCID/Scholar URL shapes; build `WhitelistedAuthor`
  from canned ORCID + OpenAlex JSON fixtures (monkeypatched HTTP); Scholar-failure
  fallback.
- `author_whitelist_bot.py`: command parsing (`add`/`remove`/`list`, junk),
  cursor advancement / idempotency, reply text, self-message skipping (monkeypatched
  Zulip client + resolver).
- `main.process_group`: with a stub whitelist, a low-scored article whose author
  matches is present in `passing`/`new_items`; non-matching low-scored article is
  not. (Existing tests must still pass.)
- Manual verification: dry run against a sample config + a real ORCID, confirm
  reply text and that the JSON file updates.

## Out of scope (YAGNI)

- Per-group whitelists.
- A two-step confirm flow for adds.
- Strict scan-time OpenAlex resolution of every article.
- Whitelisting affiliations/institutions rather than individuals.
- A live event-listener daemon.
```
