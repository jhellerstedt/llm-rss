# Catch Missed Articles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Score every unseen RSS item (no 24h cutoff), force-include method-keyword hits, and treat journal + arXiv copies as one paper for RSS and Zulip.

**Architecture:** New `paper_identity.py` (DOI/arXiv/URL keys + clustering) and `seen_articles.py` (JSON beside config). Adapter drops the time filter. Shortlist gains `method_include`. Cross-group winner and Zulip dedup use identity keys. First missing seen file bootstraps without scoring.

**Tech Stack:** Python 3, unittest, existing feedparser / OpenAlex helpers.

## Global Constraints

- No live network in tests.
- Seen file: `{config_stem}.seen_articles.json`; missing → bootstrap; corrupt → abort.
- Do not skip scoring within a run because another group already scored the DOI.
- `period` remains for feedback-control windows only.
- No fuzzy title matching.

---

### File map

| File | Role |
|------|------|
| `paper_identity.py` | `identity_keys`, `cluster_links_by_identity` |
| `seen_articles.py` | load/save, `SeenArticlesCorruptError` |
| `article_prefilter.py` | `matches_method_include`, method-priority shortlist |
| `rss_merge.py` | `GroupPassingScores.identity_keys_by_link`; cluster in `winning_group_by_link` |
| `adapter.py` | `recent_articles(hours=None)` → all entries |
| `openalex_enrich.py` | `PaperEnrichment.doi`; merge/build |
| `zulip_feedback.py` | `feedback_link_keys` / `links_announced_in_messages` via identity |
| `zulip_feedback_queue.py` | persist `doi` on enrichment JSON |
| `main.py` | bootstrap; skip seen; collect considered keys; flush end of run |
| `config.d/config.toml.example`, `README.md` | document |

### Task 1: Paper identity

Create `paper_identity.py` + `tests/test_paper_identity.py`.

Produces:

- `identity_keys(link: str, enrichment: PaperEnrichment | None = None) -> set[str]`
- `cluster_links_by_identity(link_key_sets: dict[str, set[str]]) -> list[set[str]]` (each set is normalized links in one paper)

Keys: `url:` + `normalize_link`; `doi:` lowercased (strip ACS `/5404181/Title` tail); `arxiv:` id without version; enrichment `arxiv_url` / `doi`.

### Task 2: Seen store

Create `seen_articles.py` + `tests/test_seen_articles.py`.

Produces:

- `seen_articles_path(config_path: Path) -> Path`
- `load_seen_articles(path: Path) -> tuple[set[str], bool]`  # keys, bootstrap
- `save_seen_articles(path: Path, keys: set[str]) -> None`
- `SeenArticlesCorruptError`

### Task 3: Method-priority shortlist

Extend `shortlist_for_kagi_scoring` to honor `group["method_include"]`. Tests in `tests/test_article_prefilter.py`.

### Task 4: Cross-group winner by identity

`GroupPassingScores.identity_keys_by_link: dict[str, set[str]] | None = None`. `winning_group_by_link` unions links whose key sets intersect. Tests in `tests/test_rss_merge.py`.

### Task 5: Enrichment DOI + Zulip keys

Add `doi: str | None = None` on `PaperEnrichment`. Wire OpenAlex `work["doi"]`. `feedback_link_keys` and `links_announced_in_messages` use `identity_keys`. JSON roundtrip in the queue.

### Task 6: Adapter + main + docs

`recent_articles(hours=None)` returns all entries. `main.py`: load seen; bootstrap fetches all feeds, writes keys, returns; else filter unseen, method shortlist, omit failed LLM scores from seen, cluster winners, flush seen. Document `method_include`. Add SPM phrases to local `config.toml`.

---
