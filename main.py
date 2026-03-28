import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import toml
import typer
from django.utils.feedgenerator import Rss201rev2Feed
from dotenv import load_dotenv
from pydantic import BaseModel
from tqdm import tqdm

from adapter import ArticleInfo, RSSAdapter
from kagi_client import KagiClient, DEFAULT_FASTGPT_URL, DEFAULT_SUMMARIZE_URL
from zulip_context import build_zulip_context_block, load_zulip_realms

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_FENCE = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)


class Reply(BaseModel):
    relevance: int
    impact: int
    reason: str | None = None


def to_bullets(text_list: list[str]) -> str:
    return "\n".join(f"- {item}" for item in text_list)


def extract_json_object(text: str) -> str:
    t = text.strip()
    t = _FENCE.sub("", t)
    t = re.sub(r"\s*```\s*$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        return t[start : end + 1]
    return t


def parse_reply_from_fastgpt_output(text: str, article_title: str) -> Reply:
    try:
        raw = extract_json_object(text)
        data = json.loads(raw)
        return Reply.model_validate(data)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("JSON decode failed for %r: %s; snippet=%s", article_title, e, text[:400])
        return Reply(relevance=0, impact=0, reason="decode error")


def prepare_scoring_query(article: ArticleInfo, group: dict, zulip_block: str) -> str:
    research_areas = to_bullets(group["research_areas"])
    excluded_areas = to_bullets(group["excluded_areas"])
    zulip_section = ""
    if zulip_block.strip():
        zulip_section = (
            "\n### Context from Zulip (team discussion; may be summarized)\n"
            f"{zulip_block.strip()}\n"
        )

    return f"""You are an academic paper evaluator curating an RSS feed.
Based on the title, abstract, the user's research areas, and any Zulip team context below, evaluate the paper.
Assign relevance (0-9): correlation with the research areas and team interests.
Assign impact (0-9): potential scientific value; can be high even if not highly relevant.

User research areas:
{research_areas}

Excluded areas (generally lower relevance if the work is primarily in these):
{excluded_areas}
{zulip_section}
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
) -> Reply:
    query = prepare_scoring_query(article, group, zulip_block)
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
    )


def process_group(
    group: dict,
    kagi: KagiClient,
    zulip_realms: dict,
    zulip_cfg: dict,
    dryrun: bool,
) -> None:
    rss_path = group["rss_path"]
    period = group["period"]
    relevance_threshold = group["relevance_threshold"]
    impact_threshold = group["impact_threshold"]
    concurrent_requests = group["concurrent_requests"]
    crawl_abstract = group["crawl_abstract"]
    group_name = group["name"]

    context_max_chars = int(zulip_cfg.get("context_max_chars", 12_000))
    zulip_sources = group.get("zulip_sources") or []

    zulip_block = ""
    if zulip_sources:
        if not zulip_realms:
            logger.warning(
                "group %s has zulip_sources but no Zulip realms loaded; skipping Zulip context",
                group_name,
            )
        else:
            zulip_block = build_zulip_context_block(
                zulip_sources,
                zulip_realms,
                context_max_chars,
                kagi_summarize=kagi,
            )

    new_feed = Rss201rev2Feed(
        title=f"Filtered RSS — {group_name}",
        link="myserver",
        description=f"LLM-filtered feed ({group_name})",
        language="en",
    )

    now = datetime.now(tz=timezone.utc)
    rss_urls = group["urls"]
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

    if concurrent_requests is None:
        concurrent_requests = max(1, len(recent_articles))

    def worker(art: ArticleInfo) -> Reply:
        return get_kagi_reply(art, group, kagi, zulip_block)

    with ThreadPoolExecutor(max_workers=max(1, concurrent_requests)) as pool:
        futures = [pool.submit(worker, art) for art in recent_articles]
        replies = [f.result() for f in tqdm(futures, desc=f"Kagi FastGPT ({group_name})")]

    for article, reply in zip(recent_articles, replies):
        if reply.relevance > relevance_threshold and reply.impact > impact_threshold:
            new_feed.add_item(
                title=article.title,
                link=str(article.link),
                description=f"{reply.relevance=}\n {reply.impact=}\n " + article.abstract,
                pubdate=now,
            )

    if new_feed.num_items() > 0 and not dryrun:
        Path(rss_path).parent.mkdir(parents=True, exist_ok=True)
        with open(rss_path, "w", encoding="utf-8") as f:
            new_feed.write(f, "utf-8")
        print(f"Wrote {new_feed.num_items()} items to {rss_path}")
    else:
        print(f"[{group_name}] not updated (no items or dryrun)")


def main(config_path: Path = Path("config.toml"), dryrun: bool = False) -> None:
    cfg = toml.load(config_path)
    kagi_table = cfg.get("kagi") or {}
    kagi = make_kagi_client(kagi_table)
    if not kagi.api_key:
        raise ValueError("Kagi API key missing: set [kagi] api_key or environment variable KAGI_API_KEY")

    zulip_cfg = cfg.get("zulip") or {}
    realms_path = zulip_cfg.get("realms_config_file")
    if realms_path is None:
        realms_path = os.environ.get("ZULIP_REALMS_CONFIG_FILE")
    zulip_realms = load_zulip_realms(config_file=realms_path)

    groups = expand_groups(cfg)
    for group in groups:
        print(f"--- Group: {group['name']} ---")
        process_group(group, kagi, zulip_realms, zulip_cfg, dryrun)


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
