# Catch missed journal / arXiv articles — design

Status: proposed (awaiting review)
Date: 2026-09-08

## Problem

The 2 Sep 2026 Nano Letters paper *Direct Observation of the Zigzag Edge
States of a Supramolecular Diatomic Kagome Lattice*
(`10.1021/acs.nanolett.6c03195`, arXiv `2609.02567`) is a textbook SPM
match (STM of zigzag edge states on Pb(111)). The current pipeline would
not have posted it to `#SPM` on an 8 Sep run, for three independent reasons:

1. **Recency.** Each group keeps only feed items newer than `period`
   hours (24 for SPM). There is no memory of what was already scored.
2. **Shortlist.** SPM often has ~200–270 items in a day and scores only
   `prefilter_max_candidates` (20) by local keyword overlap. An STM paper
   can lose to quantum-computing keyword noise.
3. **Identity.** The ACS HTML/DOI URL and `arxiv.org/abs/2609.02567` are
   different links. Cross-group RSS/Zulip assignment is by normalized URL,
   so `#science` (`acs_journals` / `arxiv_condmat`) and `#SPM` can both
   claim the same work.

Motivating article is **not** backfilled. The change is so the **next**
paper like it is eligible, force-scored for SPM, and owned by one group.

## Decisions (locked with user)

| Topic | Decision |
|-------|----------|
| Scope of miss modes | Recency, then shortlist, then identity |
| Recency gate | No time filter. Every item still in a publisher RSS feed is eligible until marked seen |
| Seen-set | Persistent JSON beside config; score each paper at most once |
| Groups | All `[[groups]]` entries |
| First run | If the seen file is **missing**, mark everything currently in feeds as seen **without scoring** |
| Shortlist | Force-include `method_include` hits; fill remaining cap by local score; mark all considered items seen |
| Identity | One paper across seen-set, cross-group RSS ownership, and Zulip queue |
| Within a run | Groups still score their own copies; do **not** skip scoring just because an earlier group already scored the DOI (otherwise `acs_journals` would eat STM papers before SPM) |
| Title matching | Out of scope. No fuzzy title clustering |

## Architecture

| Unit | File | Responsibility |
|------|------|----------------|
| Identity | `paper_identity.py` (new) | DOI / arXiv / URL keys; union-find cluster |
| Seen store | `seen_articles.py` (new) | Load/save JSON; bootstrap-if-missing; end-of-run merge |
| Shortlist | `article_prefilter.py` | Method-priority shortlist |
| Fetch | `adapter.py` / `main.py` | All feed entries; skip seen; flush seen after all groups |
| RSS winner | `rss_merge.py` | Winner by identity cluster, not URL alone |
| Zulip | `zulip_feedback.py` | `feedback_link_keys` uses the same helper |
| Config / docs | `config.d/config.toml.example`, `README.md` | `method_include`; `period` is not an eligibility window |

### Data flow

```
main() loads seen file (missing → bootstrap_mode)
  for each group:
      fetch ALL entries from each RSS URL (no hours filter)
      if bootstrap_mode:
          collect identity keys; do not score
      else:
          drop items whose any identity key is already seen
          shortlist: method_include hits, then local-score fill
          LLM-score shortlist (failures are not marked seen)
          OpenAlex enrich (adds DOI / arXiv aliases)
          collect considered keys (shortlisted and not)
  after all groups:
      cluster passing items by identity
      assign RSS + Zulip winner per cluster
      if bootstrap_mode or normal run:
          merge collected keys into seen file and write
          (omit keys from LLM score failures)
```

## Eligibility and seen file

Path: `{config_stem}.seen_articles.json` next to the TOML (gitignored
with other `config.d/**/*.json`).

`period` remains in config for **feedback-control** lookback only. It is
not used to drop RSS items.

Adapter: `recent_articles(hours=None)` returns every parsed entry (no
time filter). `main.py` always passes `hours=None`. Nature’s extra +24h
fudge is unused when `hours` is None.

### Bootstrap

- **Missing file** → bootstrap: fetch every configured feed, record
  identity keys for every entry, write the file, **score nothing**.
- **Corrupt JSON** → log ERROR and **abort the run**. Do not bootstrap
  (that would hide every item currently in feeds).
- **Empty-but-valid file** (`{"version": 1, "keys": []}`) is not missing;
  score new items as usual.

Deleting the seen file is an operator action: it re-bootstraps and will
not score items already sitting in feeds.

### What gets marked seen

| Event | Seen? |
|-------|--------|
| Bootstrap collected from a successful feed parse | Yes |
| Item LLM-scored (pass or below threshold) | Yes |
| Item considered but not shortlisted | Yes |
| LLM score failed (batch and single-article fallback both failed) | **No** |
| Feed fetch / parse failed | **No** (that feed’s items unknown) |
| Crash before end-of-run flush | **No** (retry next run) |

Flush once at **end of run** so later groups in the same run can still
score the same DOI.

### File format

```json
{
  "version": 1,
  "keys": [
    "doi:10.1021/acs.nanolett.6c03195",
    "arxiv:2609.02567",
    "url:https://dx.doi.org/10.1021/acs.nanolett.6c03195"
  ]
}
```

Keys are a flat sorted unique set. No per-group split (identity is
global). Version bump required if the shape changes.

## Paper identity

`identity_keys(link: str, enrichment: PaperEnrichment | None = None) -> set[str]`

Union of:

- `url:` + `normalize_link(link)`
- `doi:` + lowercased DOI from the URL when present (`dx.doi.org`,
  `pubs.acs.org/doi/…`, `10.1021/…` in the path). Reuse
  `extract_doi_from_link` in `openalex_enrich.py` (or move the regexes
  next to this helper to avoid import cycles).
- `arxiv:` + id without version (`2609.02567`) from abs/pdf URLs or
  DataCite `10.48550/arXiv.…`
- If `enrichment` is present: `arxiv:` from `enrichment.arxiv_url`
- Add optional `doi: str | None` on `PaperEnrichment` when OpenAlex
  resolves a work DOI, and include `doi:` from that field

**Same run clustering.** After all groups score and enrich, union-find:
two items are the same paper if their key sets intersect. Extend
`winning_group_by_link` so every normalized URL in a cluster maps to the
same winning group (highest relevance, then impact, then group name —
same tie-break as today). `filter_feed_items_for_group` and
`filter_to_group_winning_links` use that map.

**Later runs.** End-of-run seen write stores every key in each considered
cluster. An arXiv abs URL the next day is skipped if the DOI or arXiv id
was recorded from the journal copy.

**Same-day journal + arXiv without a shared key.** If OpenAlex has not
linked them and the arXiv RSS entry has no journal DOI, they stay two
papers until an alias appears. No fuzzy title matching.

**Zulip.** `feedback_link_keys` returns `normalize_link` of each URL-like
key plus the existing `preferred_public_link` behavior, implemented via
`identity_keys` so the queue does not post both copies.

## Method-priority shortlist

Optional per-group list:

```toml
method_include = [
    "STM",
    "STS",
    "scanning tunneling",
    "ncAFM",
    "qPlus",
    "scanning probe",
]
```

Match **case-insensitively** in `title + " " + abstract`. Phrase match
(substring), not whole-word only, so `scanning tunneling microscopy`
matches `scanning tunneling`.

If `method_include` is missing or empty: today’s
`shortlist_for_kagi_scoring` only.

Otherwise, for unseen items:

1. **Method hits** — items matching any phrase. If more method hits than
   `prefilter_max_candidates`, keep the highest `local_article_score`
   among **method hits only** (they do not compete with non-method items).
2. **Fill** — remaining slots (if any) from non-method items via
   `shortlist_for_kagi_scoring`.
3. Non-shortlisted unseen items are still marked seen at end of run.

SPM config should ship with STM/ncAFM phrases. Other groups may omit the
key. Example file documents the field; live `config.toml` is local.

## Error handling

- Missing seen file → bootstrap (not an error).
- Corrupt seen file → abort `main()` after logging; Zulip run-error
  digest still runs in `finally`.
- Individual RSS URL failure → log, skip that URL, do not invent seen
  keys for it.
- OpenAlex failure → still cluster on URL/DOI/arXiv from the link;
  enrichment aliases omitted.
- Credit preflight unchanged: it still uses
  `ceil(prefilter_max_candidates / batch_size)` per group as the scoring
  upper bound.

## Tests (no live network)

- Identity keys: ACS HTML article URL, `http://dx.doi.org/10.1021/…`,
  `https://arxiv.org/abs/2609.02567`, DataCite arXiv DOI.
- Cluster: journal URL + arXiv abs sharing a DOI → one winner; SPM
  relevance 8 beats `acs_journals` relevance 6.
- Seen: missing file bootstraps; second pass skips those keys; LLM
  failure keys omitted from the write.
- Shortlist: STM abstract force-included; off-topic item not preferred
  over method hits; when method hits exceed cap, only method hits occupy
  the shortlist.
- Zulip: identity keys suppress a second post for the arXiv alias.

## Out of scope

- Backfilling the Sep 2026 Nano Letters paper (bootstrap marks it seen if
  it is still in the feed).
- Fuzzy title / author clustering.
- Changing Zulip queue drain / reaction gating.
- Raising default `prefilter_max_candidates`.
- Per-group seen files.
