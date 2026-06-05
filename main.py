import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import toml
import typer
from django.utils.feedgenerator import Rss201rev2Feed
from dotenv import load_dotenv
from tqdm import tqdm

from adapter import ArticleInfo, RSSAdapter
from article_prefilter import shortlist_for_kagi_scoring
from fastgpt_reply import (
    Reply,
    parse_reply_from_fastgpt_output,
    parse_reply_from_openrouter_output,
)
from kagi_client import KagiClient, DEFAULT_FASTGPT_URL, DEFAULT_SUMMARIZE_URL
from openrouter_client import OpenRouterClient, get_openrouter_usage, reset_openrouter_usage
from openalex_enrich import (
    PaperEnrichment,
    apply_kagi_metadata_backfill,
    batch_enrich_articles,
    format_enrichment_for_feed,
)
from api_usage import log_api_usage_summary, reset_api_usage_stats
from kagi_batch_scoring import score_article_batch_with_kagi
from openrouter_batch_scoring import score_article_batch_with_openrouter
from kagi_quota import (
    KagiSessionQuotaExceeded,
    MAX_KAGI_INVOCATIONS_PER_RUN,
    log_kagi_quota_status,
    plan_scoring_budget,
    remaining_kagi_invocations,
    reset_kagi_session_quota,
)
from rss_merge import (
    FeedItem,
    GroupPassingScores,
    filter_feed_items_for_group,
    load_persisted_feed_items,
    merge_feed_history,
    normalize_link,
    winning_group_by_link,
)
from zulip_context import build_zulip_context_and_messages, load_zulip_realms
from zulip_feedback import (
    GroupFeedbackCandidates,
    filter_to_group_winning_links,
    format_feedback_prompt_snippet,
    load_feedback_state_for_group,
    post_feedback_ranking_for_new_items,
    select_top_ranked_for_feedback_posts,
)
from zulip_feedback_queue import (
    dispatch_feedback_ranking_queue_once,
    enqueue_feedback_ranking_for_group,
)
from journal_venue import VenueBucket, tracked_venues_from_group_urls
from zulip_journal_suggestions import (
    DEFAULT_DOMAIN_DENYLIST,
    apex_domains_from_nested,
    curate_group_research_lists_with_kagi,
    curate_group_research_lists_with_openrouter,
    filter_academic_journal_domains_with_kagi,
    filter_academic_journal_domains_with_openrouter,
    filter_nested_by_allowed_domains,
    format_missing_journals_message_nested,
    merge_journal_suggestion_maps,
    missing_venues_by_section_from_messages,
    new_feed_urls_from_filtered_nested,
)
from zulip_journal_weekly_summary import maybe_post_weekly_journal_config_summary

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Directory containing this file (repo root). Relative rss_path values in config
# resolve here so feeds are written under the repo regardless of process CWD.
REPO_ROOT = Path(__file__).resolve().parent


def resolve_rss_path(rss_path: str | os.PathLike[str]) -> str:
    p = Path(rss_path)
    if p.is_absolute():
        return str(p)
    return str((REPO_ROOT / p).resolve())


def _normalize_feed_category(value: object) -> str | None:
    """Single token from config (first word) for stable RSS / site grouping."""
    s = str(value or "").strip()
    if not s:
        return None
    return s.split()[0]


def _format_feed_description(group_name: str, category: str | None) -> str:
    base = f"LLM-filtered feed ({group_name})"
    if not category:
        return base
    return f"{base} — category: {category}"


def to_bullets(text_list: list[str]) -> str:
    return "\n".join(f"- {item}" for item in text_list)


def split_scoring_query(query: str) -> tuple[str, str]:
    """Split a scoring prompt at the ### Article boundary (system / user messages)."""
    marker = "### Article"
    idx = query.find(marker)
    if idx < 0:
        return query.strip(), ""
    return query[:idx].strip(), query[idx:].strip()


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


def get_openrouter_reply(
    article: ArticleInfo,
    group: dict,
    openrouter: OpenRouterClient,
    zulip_block: str,
    feedback_snippet: str = "",
) -> Reply:
    query = prepare_scoring_query(article, group, zulip_block, feedback_snippet)
    system, user = split_scoring_query(query)
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user or query})
    output = openrouter.chat_completion(messages)
    return parse_reply_from_openrouter_output(output, article.title)


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
        "rss_path": resolve_rss_path(cfg.get("rss_path", "data/rss.xml")),
        "feed_link": str(cfg.get("feed_link", "myserver")),
        "feed_category": _normalize_feed_category(
            cfg.get("feed_category") or cfg.get("category")
        ),
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
                "rss_path": resolve_rss_path(
                    g.get("rss_path", cfg.get("rss_path", "data/rss.xml"))
                ),
                "feed_link": str(g.get("feed_link", cfg.get("feed_link", "myserver"))),
                "feed_category": _normalize_feed_category(
                    g.get("feed_category")
                    or g.get("category")
                    or cfg.get("feed_category")
                    or cfg.get("category")
                ),
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
        min_seconds_between_requests=float(
            kagi_table.get("min_seconds_between_requests", 2.0)
        ),
    )


def make_openrouter_client(openrouter_table: dict | None) -> OpenRouterClient | None:
    if not openrouter_table:
        return None
    api_key = (
        openrouter_table.get("api_key") or os.environ.get("OPENROUTER_API_KEY", "")
    ).strip()
    if not api_key:
        return None
    return OpenRouterClient(
        api_key=api_key,
        model=openrouter_table.get("model"),
        timeout=float(openrouter_table.get("timeout", 60)),
        site_url=str(openrouter_table.get("site_url", "")),
        site_name=str(openrouter_table.get("site_name", "llm-rss")),
    )


def routes_to_openrouter(
    route_to_openrouter: list[str] | None,
    kind: str,
    *,
    openrouter: OpenRouterClient | None,
) -> bool:
    return openrouter is not None and kind in (route_to_openrouter or [])


@dataclass
class GroupRunResult:
    group_name: str
    rss_path: str
    feed_link: str
    feed_category: str | None
    rss_max_items: int
    new_items: list[FeedItem]
    n_scored: int
    link_scores: list[tuple[str, int, int]]
    feedback: GroupFeedbackCandidates | None


def _write_group_rss(
    run: GroupRunResult,
    merged: list[FeedItem],
    dryrun: bool,
    *,
    n_new_this_run: int,
) -> None:
    n_kept = len(merged)
    if n_kept > 0 and not dryrun:
        rss_path = Path(run.rss_path)
        rss_path.parent.mkdir(parents=True, exist_ok=True)
        feed_title = run.group_name.replace("_", " ").strip().title()
        new_feed = Rss201rev2Feed(
            title=feed_title,
            link=run.feed_link,
            description=_format_feed_description(run.group_name, run.feed_category),
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
        with open(rss_path, "w", encoding="utf-8") as f:
            new_feed.write(f, "utf-8")
        print(
            f"Wrote {n_kept} item(s) to {run.rss_path} "
            f"({n_new_this_run} passed threshold this run, cap={run.rss_max_items})"
        )
    elif dryrun:
        print(
            f"[{run.group_name}] dry run — would write {n_kept} item(s) to {run.rss_path} "
            f"({n_new_this_run} passed threshold this run, scored {run.n_scored} in period, "
            f"cap={run.rss_max_items})"
        )
    elif n_kept == 0:
        print(
            f"[{run.group_name}] no XML written — no persisted items and nothing passed "
            f"threshold this run ({run.rss_path} unchanged)"
        )


def _dispatch_group_feedback_posts(
    batch: GroupFeedbackCandidates,
    config_path: Path,
    zulip_cfg: dict,
    zulip_realms: dict,
    dryrun: bool,
    *,
    single_author_impact_penalty: int,
) -> None:
    """Post or enqueue up to two feedback-ranking messages for one group."""
    feedback_post_links = select_top_ranked_for_feedback_posts(
        batch.title_link_scores,
        single_author_impact_penalty=single_author_impact_penalty,
    )
    if not feedback_post_links:
        return
    use_queue = bool(zulip_cfg.get("feedback_ranking_use_queue"))
    if use_queue:
        logger.info(
            "Zulip feedback ranking: queuing up to %d article(s) for group %s "
            "(hourly dispatch via --dispatch-feedback-queue)",
            len(feedback_post_links),
            batch.group_name,
        )
        enqueue_feedback_ranking_for_group(
            config_path,
            zulip_cfg,
            batch.zulip_sources,
            batch.messages_by_pair,
            feedback_post_links,
            group_name=batch.group_name,
            dryrun=dryrun,
        )
    else:
        logger.info(
            "Zulip feedback ranking: posting up to %d article(s) for group %s "
            "(by relevance, impact)",
            len(feedback_post_links),
            batch.group_name,
        )
        post_feedback_ranking_for_new_items(
            batch.zulip_sources,
            zulip_realms,
            messages_by_pair=batch.messages_by_pair,
            titles_and_links=feedback_post_links,
            dryrun=dryrun,
        )


def process_group(
    group: dict,
    kagi: KagiClient,
    zulip_realms: dict,
    zulip_cfg: dict,
    openalex_cfg: dict,
    dryrun: bool,
    config_path: Path,
    *,
    kagi_prefilter_cap: int = 20,
    kagi_batch_size: int = 5,
    openrouter: OpenRouterClient | None = None,
    route_to_openrouter: list[str] | None = None,
) -> GroupRunResult:
    rss_path = group["rss_path"]
    feed_link = str(group.get("feed_link", "myserver"))
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
            summarize_client = (
                openrouter
                if routes_to_openrouter(
                    route_to_openrouter, "summarize", openrouter=openrouter
                )
                else kagi
            )
            zulip_block, zulip_msgs = build_zulip_context_and_messages(
                zulip_sources,
                zulip_realms,
                context_max_chars,
                kagi_summarize=summarize_client,
            )

            # Journal suggestions are merged into config.toml once per run (see main()).

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
    use_openrouter_scoring = routes_to_openrouter(
        route_to_openrouter, "scoring", openrouter=openrouter
    )
    if use_openrouter_scoring:
        batch_sz = max(1, bsz)
        shortlist_n = min(cap, n_art)
    else:
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
    scoring_label = "OpenRouter" if use_openrouter_scoring else "Kagi"
    logger.info(
        "[%s] %s batched scoring: shortlist %d/%d, batch_size=%d",
        group_name,
        scoring_label,
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

    batch_desc = (
        f"OpenRouter batches ({group_name})"
        if use_openrouter_scoring
        else f"Kagi FastGPT batches ({group_name})"
    )
    for i in tqdm(range(0, len(shortlisted), batch_sz), desc=batch_desc):
        chunk = shortlisted[i : i + batch_sz]
        batch_items: list[tuple[str, ArticleInfo, str]] = []
        for j, art in enumerate(chunk):
            bid = f"A{j + 1}"
            fb = format_feedback_prompt_snippet(str(art.link), feedback_signals)
            batch_items.append((bid, art, fb))
        try:
            if use_openrouter_scoring:
                assert openrouter is not None
                parsed = score_article_batch_with_openrouter(
                    openrouter, batch_items, group, zulip_block
                )
            else:
                parsed = score_article_batch_with_kagi(
                    kagi, batch_items, group, zulip_block
                )
        except Exception as e:
            logger.warning("%s batch scoring failed: %s", scoring_label, e)
            parsed = {}
        for bid, art, _ in batch_items:
            r = parsed.get(bid)
            if r is None:
                try:
                    fb = format_feedback_prompt_snippet(str(art.link), feedback_signals)
                    if use_openrouter_scoring:
                        assert openrouter is not None
                        r = get_openrouter_reply(
                            art,
                            group,
                            openrouter,
                            zulip_block,
                            feedback_snippet=fb,
                        )
                    else:
                        r = get_kagi_reply(
                            art, group, kagi, zulip_block, feedback_snippet=fb
                        )
                except Exception as e2:
                    logger.warning(
                        "%s single-article fallback failed: %s", scoring_label, e2
                    )
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

    feedback_batch: GroupFeedbackCandidates | None = None
    if zulip_sources and zulip_realms and passing:
        feedback_batch = GroupFeedbackCandidates(
            group_name=group_name,
            zulip_sources=zulip_sources,
            messages_by_pair=feedback_msgs_by_pair,
            title_link_scores=[
                (
                    a.title,
                    str(a.link),
                    r.relevance,
                    r.impact,
                    enrichment_by_link.get(str(a.link)),
                )
                for a, r in passing
            ],
            single_author_impact_penalty=max(
                0, int(group.get("single_author_impact_penalty", 1))
            ),
        )

    link_scores = [(str(a.link), r.relevance, r.impact) for a, r in passing]

    return GroupRunResult(
        group_name=group_name,
        rss_path=rss_path,
        feed_link=feed_link,
        feed_category=group.get("feed_category"),
        rss_max_items=rss_max_items,
        new_items=new_items,
        n_scored=len(recent_articles),
        link_scores=link_scores,
        feedback=feedback_batch,
    )


def main(config_path: Path = Path("config.toml"), dryrun: bool = False) -> None:
    reset_api_usage_stats()
    reset_kagi_session_quota()
    reset_openrouter_usage()
    try:
        cfg = toml.load(config_path)
        openalex_cfg = dict(cfg.get("openalex") or {})
        kagi_table = cfg.get("kagi") or {}
        kagi = make_kagi_client(kagi_table)
        if not kagi.api_key:
            raise ValueError(
                "Kagi API key missing: set [kagi] api_key or environment variable KAGI_API_KEY"
            )

        openrouter_table = cfg.get("openrouter")
        openrouter = make_openrouter_client(openrouter_table)
        route_to_openrouter = (
            list(openrouter_table.get("route_to_openrouter") or [])
            if openrouter_table
            else []
        )
        if openrouter_table and route_to_openrouter and openrouter is None:
            logger.warning(
                "[openrouter] section present with route_to_openrouter=%s but no API key; "
                "falling back to Kagi for those call types",
                route_to_openrouter,
            )
        elif openrouter is not None:
            logger.info(
                "OpenRouter enabled (model=%s); routed call types: %s",
                openrouter.model,
                route_to_openrouter or "(none)",
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

        # Per-group index -> untracked venues from that group's Zulip pulls (for config.toml updates).
        suggestions_by_group_idx: dict[int, dict[str, dict[str, VenueBucket]]] = {}
        zulip_plain_block_by_group_idx: dict[int, str] = {}
        group_runs: list[GroupRunResult] = []
        for gi, group in enumerate(groups):
            print(f"--- Group: {group['name']} ---")
            group_runs.append(
                process_group(
                    group,
                    kagi,
                    zulip_realms,
                    zulip_cfg,
                    openalex_cfg,
                    dryrun,
                    config_path,
                    kagi_prefilter_cap=pf_cap,
                    kagi_batch_size=batch_sz,
                    openrouter=openrouter,
                    route_to_openrouter=route_to_openrouter,
                )
            )

            zulip_sources = group.get("zulip_sources") or []
            if not zulip_sources or not zulip_realms:
                continue
            try:
                context_max_chars = int((zulip_cfg or {}).get("context_max_chars", 12_000))
                block, msgs = build_zulip_context_and_messages(
                    zulip_sources,
                    zulip_realms,
                    context_max_chars,
                    kagi_summarize=None,
                )
                zulip_plain_block_by_group_idx[gi] = block
            except Exception:
                logger.exception("Failed to refetch Zulip messages for journal suggestions")
                continue

            tracked_vk = tracked_venues_from_group_urls([str(u) for u in (group.get("urls") or [])])
            missing_nested = missing_venues_by_section_from_messages(
                msgs,
                tracked_venue_keys=tracked_vk,
                denylist=DEFAULT_DOMAIN_DENYLIST,
            )
            if not missing_nested:
                continue
            dest = suggestions_by_group_idx.setdefault(gi, {})
            merge_journal_suggestion_maps(dest, missing_nested)

        passing_batches = [
            GroupPassingScores(r.group_name, r.link_scores)
            for r in group_runs
            if r.link_scores
        ]
        link_winners = (
            winning_group_by_link(passing_batches) if passing_batches else {}
        )

        for run in group_runs:
            persisted = load_persisted_feed_items(Path(run.rss_path))
            n_persisted_before = len(persisted)
            n_new_before = len(run.new_items)
            persisted = filter_feed_items_for_group(
                persisted, run.group_name, link_winners
            )
            new_items = filter_feed_items_for_group(
                run.new_items, run.group_name, link_winners
            )
            dropped = (n_persisted_before - len(persisted)) + (
                n_new_before - len(new_items)
            )
            if dropped:
                logger.info(
                    "[%s] cross-group RSS dedup: %d paper(s) kept in another group's feed "
                    "(higher relevance)",
                    run.group_name,
                    dropped,
                )
            merged = merge_feed_history(persisted, new_items, run.rss_max_items)
            _write_group_rss(run, merged, dryrun, n_new_this_run=len(new_items))

        feedback_batches = [r.feedback for r in group_runs if r.feedback]
        if feedback_batches:
            for batch in feedback_batches:
                before = len(batch.title_link_scores)
                batch.title_link_scores = filter_to_group_winning_links(
                    batch, link_winners
                )
                dropped = before - len(batch.title_link_scores)
                if dropped:
                    logger.info(
                        "[%s] cross-group dedup: %d paper(s) assigned to another group "
                        "(higher relevance)",
                        batch.group_name,
                        dropped,
                    )
                if not batch.title_link_scores:
                    continue
                _dispatch_group_feedback_posts(
                    batch,
                    config_path,
                    zulip_cfg,
                    zulip_realms,
                    dryrun,
                    single_author_impact_penalty=batch.single_author_impact_penalty,
                )

        # One journal-domain filter for the run, then merge feeds + curated lists into config.toml.
        if suggestions_by_group_idx and kagi:
            all_domains: list[str] = sorted(
                {
                    d
                    for nested in suggestions_by_group_idx.values()
                    for d in apex_domains_from_nested(nested)
                }
            )
            use_openrouter_domains = routes_to_openrouter(
                route_to_openrouter, "domains", openrouter=openrouter
            )
            try:
                if use_openrouter_domains:
                    assert openrouter is not None
                    allowed, _reasons = filter_academic_journal_domains_with_openrouter(
                        openrouter, all_domains
                    )
                else:
                    allowed, _reasons = filter_academic_journal_domains_with_kagi(
                        kagi, all_domains
                    )
                allowed_set = set(allowed)
            except Exception:
                provider = "OpenRouter" if use_openrouter_domains else "Kagi"
                logger.exception(
                    "%s journal-domain filter failed; skipping journal suggestion config updates",
                    provider,
                )
                allowed_set = set()

            if allowed_set:
                config_changed = False
                research_area_kagi_skip_logged = False
                for gi, group in enumerate(groups):
                    nested = suggestions_by_group_idx.get(gi)
                    if not nested:
                        continue
                    filtered_nested = filter_nested_by_allowed_domains(nested, allowed_set)
                    if not filtered_nested:
                        continue

                    if cfg.get("groups"):
                        gtable = cfg["groups"][gi]
                    else:
                        gtable = cfg

                    urls_before = list(gtable.get("urls") or [])
                    new_urls = new_feed_urls_from_filtered_nested(
                        filtered_nested,
                        urls_before,
                    )
                    journals_md = format_missing_journals_message_nested(filtered_nested)
                    zulip_excerpt = zulip_plain_block_by_group_idx.get(gi, "")

                    curated = None
                    use_openrouter_curate = routes_to_openrouter(
                        route_to_openrouter, "curate", openrouter=openrouter
                    )
                    if use_openrouter_curate:
                        try:
                            assert openrouter is not None
                            curated = curate_group_research_lists_with_openrouter(
                                openrouter,
                                group_name=str(group.get("name", "unnamed")),
                                research_areas=list(gtable.get("research_areas") or []),
                                excluded_areas=list(gtable.get("excluded_areas") or []),
                                journals_markdown=journals_md,
                                zulip_excerpt=zulip_excerpt,
                            )
                        except Exception:
                            logger.exception(
                                "OpenRouter research-area curation failed; group=%s",
                                group.get("name"),
                            )
                    elif remaining_kagi_invocations() < 1:
                        if not research_area_kagi_skip_logged:
                            logger.warning(
                                "Kagi session quota exhausted (%s invocations/run); skipping "
                                "research-area curation for remaining groups (feed URL updates still apply).",
                                MAX_KAGI_INVOCATIONS_PER_RUN,
                            )
                            research_area_kagi_skip_logged = True
                    else:
                        try:
                            curated = curate_group_research_lists_with_kagi(
                                kagi,
                                group_name=str(group.get("name", "unnamed")),
                                research_areas=list(gtable.get("research_areas") or []),
                                excluded_areas=list(gtable.get("excluded_areas") or []),
                                journals_markdown=journals_md,
                                zulip_excerpt=zulip_excerpt,
                            )
                        except KagiSessionQuotaExceeded:
                            if not research_area_kagi_skip_logged:
                                logger.warning(
                                    "Kagi session quota hit during research-area curation; "
                                    "skipping curation for this group and later ones (feed URL updates still apply)."
                                )
                                research_area_kagi_skip_logged = True
                        except Exception:
                            logger.exception(
                                "Kagi research-area curation failed; group=%s",
                                group.get("name"),
                            )

                    merged_urls = list(urls_before)
                    for u in new_urls:
                        if u not in merged_urls:
                            merged_urls.append(u)
                    ra_before = list(gtable.get("research_areas") or [])
                    ex_before = list(gtable.get("excluded_areas") or [])
                    ra_after, ex_after = ra_before, ex_before
                    if curated is not None:
                        ra_after, ex_after = curated

                    if merged_urls != urls_before:
                        gtable["urls"] = merged_urls
                        config_changed = True
                        logger.info(
                            "[%s] journal suggestions: added %d feed URL(s) to config",
                            group.get("name"),
                            len(merged_urls) - len(urls_before),
                        )
                    if curated is not None and (
                        ra_after != ra_before or ex_after != ex_before
                    ):
                        gtable["research_areas"] = ra_after
                        gtable["excluded_areas"] = ex_after
                        config_changed = True
                        logger.info(
                            "[%s] journal suggestions: updated research_areas / excluded_areas in config",
                            group.get("name"),
                        )

                if config_changed:
                    if dryrun:
                        logger.info(
                            "[dry run] would rewrite %s with journal-suggestion updates (feeds and/or areas)",
                            config_path,
                        )
                    else:
                        with open(config_path, "w", encoding="utf-8") as f:
                            toml.dump(cfg, f)
                        logger.info(
                            "Wrote journal-suggestion updates (feeds and/or areas) to %s",
                            config_path,
                        )

        try:
            maybe_post_weekly_journal_config_summary(
                config_path=config_path,
                cfg=cfg,
                zulip_realms=zulip_realms,
                zulip_cfg=zulip_cfg,
                dryrun=dryrun,
            )
        except Exception:
            logger.exception("Weekly journal config summary step failed")
    finally:
        log_api_usage_summary(logger)
        log_kagi_quota_status(logger)
        or_usage = get_openrouter_usage()
        if or_usage.get("calls", 0) > 0:
            logger.info(
                "OpenRouter usage: calls=%s, input_tokens=%s, output_tokens=%s",
                or_usage.get("calls", 0),
                or_usage.get("input_tokens", 0),
                or_usage.get("output_tokens", 0),
            )


def _dispatch_feedback_queue_configs(
    config_dir: Path, config_path: Optional[Path], dryrun: bool
) -> None:
    """Load Zulip credentials and drain at most one queued feedback post per stream."""
    if config_path is None:
        paths = sorted(config_dir.glob("*.toml"))
        for p in paths:
            print(f"{p}:")
            _dispatch_feedback_queue_single(p, dryrun)
    else:
        _dispatch_feedback_queue_single(config_path, dryrun)


def _dispatch_feedback_queue_single(config_path: Path, dryrun: bool) -> None:
    cfg = toml.load(config_path)
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
    dispatch_feedback_ranking_queue_once(
        config_path, cfg, zulip_realms, dryrun=dryrun
    )


def _main(
    config_dir: Path = Path("config.d"),
    config_path: Optional[Path] = typer.Option(
        None,
        "--config-path",
        help="Single TOML config file (default: process every *.toml in config-dir).",
    ),
    dryrun: bool = False,
    dispatch_feedback_queue: bool = typer.Option(
        False,
        "--dispatch-feedback-queue",
        help="Only drain the feedback-ranking JSON queue (one post per Zulip stream per run); "
        "use hourly cron alongside the normal feed run.",
    ),
) -> None:
    if dispatch_feedback_queue:
        reset_api_usage_stats()
        try:
            _dispatch_feedback_queue_configs(config_dir, config_path, dryrun)
        finally:
            log_api_usage_summary(logger)
        return
    if config_path is None:
        paths = sorted(config_dir.glob("*.toml"))
        for p in paths:
            print(f"{p}:")
            main(config_path=p, dryrun=dryrun)
    else:
        main(config_path=config_path, dryrun=dryrun)


if __name__ == "__main__":
    typer.run(_main)
