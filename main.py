import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import toml
import typer
from django.utils.feedgenerator import Rss201rev2Feed
from dotenv import load_dotenv
from tqdm import tqdm

from adapter import ArticleInfo, RSSAdapter
from article_prefilter import shortlist_for_kagi_scoring
from fastgpt_reply import Reply, parse_reply_from_fastgpt_output
from kagi_client import KagiClient, DEFAULT_FASTGPT_URL, DEFAULT_SUMMARIZE_URL
from openalex_enrich import (
    PaperEnrichment,
    apply_kagi_metadata_backfill,
    batch_enrich_articles,
    format_enrichment_for_feed,
)
from api_usage import log_api_usage_summary, reset_api_usage_stats
from kagi_batch_scoring import score_article_batch_with_kagi
from kagi_quota import log_kagi_quota_status, plan_scoring_budget, reset_kagi_session_quota
from rss_merge import FeedItem, load_persisted_feed_items, merge_feed_history, normalize_link
from zulip_context import build_zulip_context_and_messages, load_zulip_realms
from zulip_feedback import (
    format_feedback_prompt_snippet,
    load_feedback_state_for_group,
    post_feedback_ranking_for_new_items,
    select_top_ranked_for_feedback_posts,
)
from zulip_journal_suggestions import (
    DEFAULT_DOMAIN_DENYLIST,
    domain_counts_from_zulip_messages,
    filter_academic_journal_domains_with_kagi,
    format_missing_journals_message,
    missing_domain_counts,
    post_missing_journals_suggestions,
    tracked_domains_from_group_urls,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def to_bullets(text_list: list[str]) -> str:
    return "\n".join(f"- {item}" for item in text_list)


def prepare_scoring_query(
    article: ArticleInfo,
    group: dict,
    zulip_block: str,
    feedback_snippet: str = "",
) -> str:
    research_areas = to_bullets(group["research_areas"])
    excluded_areas = to_bullets(group["excluded_areas"])
    zulip_section = ""
    if zulip_block.strip():
        zulip_section = (
            "\n### Context from Zulip (team discussion; may be summarized)\n"
            f"{zulip_block.strip()}\n"
        )
    fb = feedback_snippet.strip()

    return f"""You are an academic paper evaluator curating an RSS feed.
Based on the title, abstract, the user's research areas, and any Zulip team context below, evaluate the paper.
Assign relevance (0-9): correlation with the research areas and team interests.
Assign impact (0-9): potential scientific value; can be high even if not highly relevant.

User research areas:
{research_areas}

Excluded areas (generally lower relevance if the work is primarily in these):
{excluded_areas}
{zulip_section}{fb}
### Article
title: {article.title}
abstract: {article.abstract}

Respond with ONLY a single JSON object (no markdown code fences, no other text) with exactly these keys:
"relevance" (integer 0-9), "impact" (integer 0-9), "reason" (string, optional short justification).
Example: {{"relevance": 6, "impact": 5, "reason": "..."}}
"""


def get_kagi_reply(
    article: ArticleInfo,
    group: dict,
    kagi: KagiClient,
    zulip_block: str,
    feedback_snippet: str = "",
) -> Reply:
    query = prepare_scoring_query(article, group, zulip_block, feedback_snippet)
    output = kagi.fastgpt_query(query)
    return parse_reply_from_fastgpt_output(output, article.title)


def _legacy_group(cfg: dict) -> dict:
    required = ("urls", "research_areas", "excluded_areas")
    for k in required:
        if k not in cfg:
            raise ValueError(
                f"Missing '{k}' in config. Use [[groups]] entries or legacy top-level keys."
            )
    return {
        "name": cfg.get("group_name", "default"),
        "urls": cfg["urls"],
        "research_areas": cfg["research_areas"],
        "excluded_areas": cfg["excluded_areas"],
        "rss_path": cfg.get("rss_path", "data/rss.xml"),
        "rss_max_items": int(cfg.get("rss_max_items", 25)),
        "period": cfg.get("period", 24),
        "relevance_threshold": cfg.get("relevance_threshold", 5),
        "impact_threshold": cfg.get("impact_threshold", 3),
        "concurrent_requests": cfg.get("concurrent_requests"),
        "crawl_abstract": cfg.get("crawl_abstract", False),
        "zulip_sources": cfg.get("zulip_sources", []),
    }


def expand_groups(cfg: dict) -> list[dict]:
    if cfg.get("groups"):
        groups = []
        for g in cfg["groups"]:
            entry = {
                "name": g.get("name", "unnamed"),
                "urls": g["urls"],
                "research_areas": g["research_areas"],
                "excluded_areas": g["excluded_areas"],
                "rss_path": g.get("rss_path", cfg.get("rss_path", "data/rss.xml")),
                "rss_max_items": int(
                    g.get("rss_max_items", cfg.get("rss_max_items", 25))
                ),
                "period": g.get("period", cfg.get("period", 24)),
                "relevance_threshold": g.get(
                    "relevance_threshold", cfg.get("relevance_threshold", 5)
                ),
                "impact_threshold": g.get(
                    "impact_threshold", cfg.get("impact_threshold", 3)
                ),
                "concurrent_requests": g.get("concurrent_requests", cfg.get("concurrent_requests")),
                "crawl_abstract": g.get("crawl_abstract", cfg.get("crawl_abstract", False)),
                "zulip_sources": g.get("zulip_sources", []),
                "prefilter_max_candidates": g.get("prefilter_max_candidates"),
                "scoring_batch_size": g.get("scoring_batch_size"),
            }
            groups.append(entry)
        return groups
    return [_legacy_group(cfg)]


def make_kagi_client(kagi_table: dict) -> KagiClient:
    return KagiClient(
        api_key=kagi_table.get("api_key"),
        fastgpt_url=kagi_table.get("fastgpt_url", DEFAULT_FASTGPT_URL),
        summarize_url=kagi_table.get("summarize_url", DEFAULT_SUMMARIZE_URL),
        fastgpt_timeout=float(kagi_table.get("fastgpt_timeout", 120)),
        summarize_timeout=float(kagi_table.get("summarize_timeout", 180)),
        web_search=bool(kagi_table.get("web_search", True)),
        summarize_engine=str(kagi_table.get("summarize_engine", "muriel")),
        use_cache=bool(kagi_table.get("use_cache", True)),
        max_concurrent_api_requests=int(kagi_table.get("max_concurrent_api_requests", 2)),
        max_http_attempts=int(kagi_table.get("max_http_attempts", 12)),
    )


def process_group(
    group: dict,
    kagi: KagiClient,
    zulip_realms: dict,
    zulip_cfg: dict,
    openalex_cfg: dict,
    dryrun: bool,
    *,
    kagi_prefilter_cap: int = 20,
    kagi_batch_size: int = 5,
) -> None:
    rss_path = group["rss_path"]
    period = group["period"]
    relevance_threshold = group["relevance_threshold"]
    impact_threshold = group["impact_threshold"]
    crawl_abstract = group["crawl_abstract"]
    group_name = group["name"]
    openalex_enabled = bool(openalex_cfg.get("enabled", True))
    openalex_kagi_fallback = bool(openalex_cfg.get("kagi_fallback", True))
    openalex_mailto = str(
        openalex_cfg.get("mailto") or os.environ.get("OPENALEX_MAILTO", "")
    )

    context_max_chars = int(zulip_cfg.get("context_max_chars", 12_000))
    zulip_sources = group.get("zulip_sources") or []

    zulip_block = ""
    zulip_msgs: list[dict[str, Any]] = []
    if zulip_sources:
        if not zulip_realms:
            logger.warning(
                "group %s has zulip_sources but no Zulip realms loaded; skipping Zulip context",
                group_name,
            )
        else:
            zulip_block, zulip_msgs = build_zulip_context_and_messages(
                zulip_sources,
                zulip_realms,
                context_max_chars,
                kagi_summarize=kagi,
            )

            # Journal suggestions are posted once per run (see main()).

    feedback_signals: dict[str, tuple[int, int]] = {}
    feedback_msgs_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if zulip_sources and zulip_realms:
        feedback_signals, feedback_msgs_by_pair = load_feedback_state_for_group(
            zulip_sources, zulip_realms
        )

    rss_urls = group["urls"]
    rss_max_items = group["rss_max_items"]
    recent_articles: list[ArticleInfo] = []
    article_titles: list[str] = []
    crawlers = []
    with ThreadPoolExecutor() as pool:
        for url in rss_urls:
            rss_adapter = RSSAdapter(url)
            n_article = 0
            for article in rss_adapter.recent_articles(hours=period):
                if article.title not in article_titles:
                    article_titles.append(article.title)
                    recent_articles.append(article)
                    if crawl_abstract:
                        crawlers.append(pool.submit(rss_adapter.crawl_abstract, article=article))
                    n_article += 1
            print(f"{n_article} articles to process on {url}.")

    for crawler in tqdm(crawlers, desc="waiting for crawlers"):
        crawler.result()

    cap = int(group.get("prefilter_max_candidates") or kagi_prefilter_cap)
    bsz = int(group.get("scoring_batch_size") or kagi_batch_size)
    n_art = len(recent_articles)
    shortlist_n, batch_sz = plan_scoring_budget(
        n_art,
        prefilter_cap=cap,
        batch_size=bsz,
    )
    if shortlist_n == 0 and n_art > 0:
        logger.warning(
            "[%s] Kagi scoring shortlist empty (quota/reserve); all articles get score 0",
            group_name,
        )
    shortlisted = shortlist_for_kagi_scoring(
        recent_articles,
        group,
        shortlist_n,
        feedback_signals or None,
    )
    shortlisted_norm = {normalize_link(str(a.link)) for a in shortlisted}
    logger.info(
        "[%s] Kagi batched scoring: shortlist %d/%d, batch_size=%d",
        group_name,
        len(shortlisted),
        n_art,
        batch_sz,
    )

    link_to_reply: dict[str, Reply] = {}
    for art in recent_articles:
        if normalize_link(str(art.link)) not in shortlisted_norm:
            link_to_reply[str(art.link)] = Reply(
                relevance=0,
                impact=0,
                reason="not shortlisted for Kagi scoring",
            )

    for i in tqdm(
        range(0, len(shortlisted), batch_sz),
        desc=f"Kagi FastGPT batches ({group_name})",
    ):
        chunk = shortlisted[i : i + batch_sz]
        batch_items: list[tuple[str, ArticleInfo, str]] = []
        for j, art in enumerate(chunk):
            bid = f"A{j + 1}"
            fb = format_feedback_prompt_snippet(str(art.link), feedback_signals)
            batch_items.append((bid, art, fb))
        try:
            parsed = score_article_batch_with_kagi(
                kagi, batch_items, group, zulip_block
            )
        except Exception as e:
            logger.warning("Kagi batch scoring failed: %s", e)
            parsed = {}
        for bid, art, _ in batch_items:
            r = parsed.get(bid)
            if r is None:
                try:
                    fb = format_feedback_prompt_snippet(str(art.link), feedback_signals)
                    r = get_kagi_reply(art, group, kagi, zulip_block, feedback_snippet=fb)
                except Exception as e2:
                    logger.warning("Kagi single-article fallback failed: %s", e2)
                    r = Reply(
                        relevance=0,
                        impact=0,
                        reason="batch parse miss and fallback failed",
                    )
            link_to_reply[str(art.link)] = r

    replies: list[Reply] = [
        link_to_reply.get(
            str(art.link),
            Reply(relevance=0, impact=0, reason="missing reply"),
        )
        for art in recent_articles
    ]

    passing: list[tuple[ArticleInfo, Reply]] = [
        (article, reply)
        for article, reply in zip(recent_articles, replies)
        if reply.relevance > relevance_threshold and reply.impact > impact_threshold
    ]

    enrichment_by_link: dict[str, PaperEnrichment | None] = {}
    if passing:
        if openalex_enabled:
            enrichment_by_link = batch_enrich_articles(
                [a for a, _ in passing],
                mailto=openalex_mailto,
            )
        else:
            enrichment_by_link = {str(a.link): None for a, _ in passing}

        if openalex_kagi_fallback:
            apply_kagi_metadata_backfill(
                enrichment_by_link,
                [a for a, _ in passing],
                kagi,
            )

    new_items: list[FeedItem] = []
    for article, reply in passing:
        meta = format_enrichment_for_feed(
            enrichment_by_link.get(str(article.link))
        ).strip()
        desc_parts = [
            f"{reply.relevance=}\n{reply.impact=}",
        ]
        if meta:
            desc_parts.append(meta)
        desc_parts.append(article.abstract)
        description = "\n\n".join(desc_parts)
        new_items.append(
            FeedItem(
                title=article.title,
                link=str(article.link),
                description=description,
                pubdate=article.updated,
                unique_id=str(article.link),
            )
        )

    if zulip_sources and zulip_realms and passing:
        feedback_post_links = select_top_ranked_for_feedback_posts(
            [(a.title, str(a.link), r.relevance, r.impact) for a, r in passing],
        )
        if feedback_post_links:
            logger.info(
                "Zulip feedback ranking: posting up to %d article(s) for group %s (by relevance, impact)",
                len(feedback_post_links),
                group_name,
            )
            post_feedback_ranking_for_new_items(
                zulip_sources,
                zulip_realms,
                messages_by_pair=feedback_msgs_by_pair,
                titles_and_links=feedback_post_links,
                dryrun=dryrun,
            )

    n_scored = len(recent_articles)
    n_new_this_run = len(new_items)
    persisted = load_persisted_feed_items(Path(rss_path))
    merged = merge_feed_history(persisted, new_items, rss_max_items)
    n_kept = len(merged)

    feed_title = group_name.replace("_", " ").strip().title()
    new_feed = Rss201rev2Feed(
        title=feed_title,
        link="myserver",
        description=f"LLM-filtered feed ({group_name})",
        language="en",
    )
    for item in merged:
        new_feed.add_item(
            title=item.title,
            link=item.link,
            description=item.description,
            pubdate=item.pubdate,
            unique_id=item.unique_id,
        )

    if n_kept > 0 and not dryrun:
        Path(rss_path).parent.mkdir(parents=True, exist_ok=True)
        with open(rss_path, "w", encoding="utf-8") as f:
            new_feed.write(f, "utf-8")
        print(
            f"Wrote {n_kept} item(s) to {rss_path} "
            f"({n_new_this_run} passed threshold this run, cap={rss_max_items})"
        )
    elif dryrun:
        print(
            f"[{group_name}] dry run — would write {n_kept} item(s) to {rss_path} "
            f"({n_new_this_run} passed threshold this run, scored {n_scored} in period, "
            f"cap={rss_max_items})"
        )
    elif n_kept == 0:
        print(
            f"[{group_name}] no XML written — no persisted items and nothing passed "
            f"threshold this run ({rss_path} unchanged)"
        )


def main(config_path: Path = Path("config.toml"), dryrun: bool = False) -> None:
    reset_api_usage_stats()
    reset_kagi_session_quota()
    try:
        cfg = toml.load(config_path)
        openalex_cfg = dict(cfg.get("openalex") or {})
        kagi_table = cfg.get("kagi") or {}
        kagi = make_kagi_client(kagi_table)
        if not kagi.api_key:
            raise ValueError(
                "Kagi API key missing: set [kagi] api_key or environment variable KAGI_API_KEY"
            )

        zulip_cfg = cfg.get("zulip") or {}
        realms_path_cfg = zulip_cfg.get("realms_config_file")
        if realms_path_cfg:
            rp = Path(realms_path_cfg)
            if not rp.is_absolute():
                rp = (config_path.parent / rp).resolve()
            realms_path = str(rp)
        else:
            realms_path = os.environ.get("ZULIP_REALMS_CONFIG_FILE")
        zulip_realms = load_zulip_realms(
            config_file=realms_path, config_dir=config_path.parent
        )

        groups = expand_groups(cfg)
        kagi_cfg = cfg.get("kagi") or {}
        pf_cap = int(kagi_cfg.get("prefilter_max_candidates", 20))
        batch_sz = int(kagi_cfg.get("scoring_batch_size", 5))

        # Aggregate journal suggestions across the full run to avoid repeated posts per group.
        suggestions_by_pair: dict[tuple[str, str], dict[str, int]] = {}
        for group in groups:
            print(f"--- Group: {group['name']} ---")
            process_group(
                group,
                kagi,
                zulip_realms,
                zulip_cfg,
                openalex_cfg,
                dryrun,
                kagi_prefilter_cap=pf_cap,
                kagi_batch_size=batch_sz,
            )

            # Collect per-group missing domains to a run-level accumulator.
            zulip_sources = group.get("zulip_sources") or []
            if not zulip_sources or not zulip_realms:
                continue
            # NOTE: We recompute from group-level zulip context already fetched inside process_group
            # would be more efficient if process_group returned it; keeping simple for now.
            # Fetch messages again but with same limits; still one message post per run after this loop.
            try:
                context_max_chars = int((zulip_cfg or {}).get("context_max_chars", 12_000))
                _block, msgs = build_zulip_context_and_messages(
                    zulip_sources,
                    zulip_realms,
                    context_max_chars,
                    kagi_summarize=None,
                )
            except Exception:
                logger.exception("Failed to refetch Zulip messages for journal suggestions")
                continue

            tracked = tracked_domains_from_group_urls([str(u) for u in (group.get("urls") or [])])
            zulip_counts = domain_counts_from_zulip_messages(msgs, denylist=DEFAULT_DOMAIN_DENYLIST)
            missing = missing_domain_counts(
                tracked_domains=tracked,
                zulip_domain_counts=zulip_counts,
            )
            if not missing:
                continue
            # Merge missing counts into each unique realm/stream destination for this group.
            from zulip_feedback import unique_realm_stream_pairs

            for pair in unique_realm_stream_pairs(zulip_sources):
                bucket = suggestions_by_pair.setdefault(pair, {})
                for d, c in missing.items():
                    bucket[d] = bucket.get(d, 0) + int(c)

        # Single Kagi API call to filter domains, then post once per destination stream for the run.
        if suggestions_by_pair and kagi:
            all_domains: list[str] = sorted({d for m in suggestions_by_pair.values() for d in m})
            try:
                allowed, _reasons = filter_academic_journal_domains_with_kagi(kagi, all_domains)
                allowed_set = set(allowed)
            except Exception:
                logger.exception("Kagi journal-domain filter failed; skipping journal suggestions post")
                allowed_set = set()

            if allowed_set:
                for (realm, stream), dom_counts in suggestions_by_pair.items():
                    filtered_counts = {d: c for d, c in dom_counts.items() if d in allowed_set}
                    if not filtered_counts:
                        continue
                    body = format_missing_journals_message(filtered_counts)
                    post_missing_journals_suggestions(
                        zulip_sources=[{"realm": realm, "stream": stream}],
                        zulip_realms=zulip_realms,
                        message=body,
                        dryrun=dryrun,
                    )
    finally:
        log_api_usage_summary(logger)
        log_kagi_quota_status(logger)


def _main(
    config_dir: Path = Path("config.d"),
    config_path: Path | None = None,
    dryrun: bool = False,
) -> None:
    if config_path is None:
        paths = sorted(config_dir.glob("*.toml"))
        for p in paths:
            print(f"{p}:")
            main(config_path=p, dryrun=dryrun)
    else:
        main(config_path=config_path, dryrun=dryrun)


if __name__ == "__main__":
    typer.run(_main)
