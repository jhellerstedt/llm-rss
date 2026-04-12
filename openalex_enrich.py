"""Resolve paper metadata from OpenAlex (h-index, first/last affiliations)."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import requests

from adapter import ArticleInfo

logger = logging.getLogger(__name__)

OPENALEX_BASE = "https://api.openalex.org"
_HTTP_HEADERS = {"User-Agent": "llm-rss/openalex-enrich"}

_ARXIV_NEW = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(?P<id>\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
_DOI = re.compile(r"(10\.\d{4,9}/[^\s?#%]+)", re.IGNORECASE)


@dataclass(frozen=True)
class AuthorMetric:
    display_name: str
    h_index: int


@dataclass(frozen=True)
class PaperEnrichment:
    top_author_name: str
    top_h_index: int
    first_affiliation: str
    last_affiliation: str

    def format_block(self) -> str:
        lines = [
            f"Highest h-index author on this paper: {self.top_author_name} "
            f"(h-index {self.top_h_index})",
        ]
        if self.first_affiliation == self.last_affiliation:
            lines.append(
                f"Institution (first & last author): {self.first_affiliation}"
            )
        else:
            lines.append(f"First author institution: {self.first_affiliation}")
            lines.append(f"Last author institution: {self.last_affiliation}")
        return "\n".join(lines)


def _norm_title(t: str) -> str:
    t = t.lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t


def _titles_match(feed_title: str, work_title: str) -> bool:
    a = _norm_title(feed_title)
    b = _norm_title(work_title)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def extract_doi_from_link(link: str) -> str | None:
    raw = unquote(link)
    m = _DOI.search(raw)
    if not m:
        return None
    return m.group(1).rstrip(".,;)")


def extract_arxiv_id(link: str) -> str | None:
    m = _ARXIV_NEW.search(link)
    return m.group("id") if m else None


def work_api_url_from_identifiers(link: str) -> str | None:
    doi = extract_doi_from_link(link)
    if doi:
        return f"{OPENALEX_BASE}/works/https://doi.org/{doi}"
    arxiv_id = extract_arxiv_id(link)
    if arxiv_id:
        return f"{OPENALEX_BASE}/works/https://doi.org/10.48550/arXiv.{arxiv_id}"
    return None


def _authors_api_path(author_openalex_id_url: str) -> str:
    tail = author_openalex_id_url.rstrip("/").split("/")[-1]
    return f"{OPENALEX_BASE}/authors/{tail}"


def _get_json(url: str, mailto: str) -> Any | None:
    params: dict[str, str | int] = {}
    if mailto:
        params["mailto"] = mailto
    try:
        r = requests.get(
            url, params=params, timeout=25, headers=_HTTP_HEADERS
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("OpenAlex request failed %s: %s", url, e)
        return None


def fetch_work(article: ArticleInfo, mailto: str) -> Any | None:
    direct = work_api_url_from_identifiers(str(article.link))
    if direct:
        data = _get_json(direct, mailto)
        if data and data.get("id"):
            return data

    title = article.title.strip()
    if not title:
        return None
    params: dict[str, str | int] = {"search": title, "per_page": 5}
    if mailto:
        params["mailto"] = mailto
    try:
        r = requests.get(
            f"{OPENALEX_BASE}/works",
            params=params,
            timeout=25,
            headers=_HTTP_HEADERS,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        logger.warning("OpenAlex works search failed for %r: %s", title[:80], e)
        return None
    for w in payload.get("results") or []:
        wt = w.get("title") or ""
        if _titles_match(article.title, wt):
            return w
    logger.info(
        "OpenAlex: no confident title match for %r (feed link %s)",
        title[:80],
        article.link,
    )
    return None


def fetch_author_metric(author_openalex_id_url: str, mailto: str) -> AuthorMetric:
    url = _authors_api_path(author_openalex_id_url)
    data = _get_json(url, mailto)
    if not data:
        return AuthorMetric(display_name="", h_index=0)
    name = str(data.get("display_name") or "").strip()
    stats = data.get("summary_stats") or {}
    try:
        h = int(stats.get("h_index") or 0)
    except (TypeError, ValueError):
        h = 0
    return AuthorMetric(display_name=name or "Unknown", h_index=max(0, h))


def affiliation_for_authorship(a: dict[str, Any]) -> str:
    insts = a.get("institutions") or []
    for inst in insts:
        dn = inst.get("display_name")
        if dn:
            return str(dn).strip()
    for aff in a.get("affiliations") or []:
        raw = aff.get("raw_affiliation_string")
        if raw:
            return str(raw).strip()
    return "Unknown"


def _first_last_authorships(
    authorships: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not authorships:
        return None, None
    first = next(
        (a for a in authorships if a.get("author_position") == "first"),
        authorships[0],
    )
    last = next(
        (a for a in authorships if a.get("author_position") == "last"),
        authorships[-1],
    )
    return first, last


def build_enrichment_for_work(
    work: dict[str, Any] | None,
    metrics_by_author_url: dict[str, AuthorMetric],
) -> PaperEnrichment | None:
    if not work:
        return None
    authorships = work.get("authorships") or []
    if not authorships:
        return None

    best_idx: int | None = None
    best_metric = AuthorMetric(display_name="Unknown", h_index=-1)

    for idx, a in enumerate(authorships):
        author = a.get("author") or {}
        aid = author.get("id")
        if not aid:
            continue
        aid = str(aid)
        m = metrics_by_author_url.get(aid, AuthorMetric(display_name="Unknown", h_index=0))
        if best_idx is None:
            best_metric = m
            best_idx = idx
        elif m.h_index > best_metric.h_index:
            best_metric = m
            best_idx = idx
        elif m.h_index == best_metric.h_index and idx < best_idx:
            best_metric = m
            best_idx = idx

    if best_idx is None:
        top_name = "Unknown"
        top_h = 0
    else:
        top_name = best_metric.display_name
        top_h = best_metric.h_index

    first_a, last_a = _first_last_authorships(authorships)
    first_aff = affiliation_for_authorship(first_a) if first_a else "Unknown"
    last_aff = affiliation_for_authorship(last_a) if last_a else "Unknown"

    return PaperEnrichment(
        top_author_name=top_name,
        top_h_index=top_h,
        first_affiliation=first_aff,
        last_affiliation=last_aff,
    )


def batch_enrich_articles(
    articles: list[ArticleInfo],
    mailto: str,
    max_work_workers: int = 3,
    max_author_workers: int = 6,
) -> dict[str, str]:
    """Map article link -> formatted metadata block (empty string if unavailable)."""
    if not articles:
        return {}

    link_to_work: dict[str, dict[str, Any] | None] = {}

    def load_work(art: ArticleInfo) -> None:
        link_to_work[str(art.link)] = fetch_work(art, mailto)

    with ThreadPoolExecutor(max_workers=max(1, max_work_workers)) as pool:
        futs = [pool.submit(load_work, art) for art in articles]
        for f in futs:
            f.result()

    author_ids: set[str] = set()
    for w in link_to_work.values():
        if not w:
            continue
        for a in w.get("authorships") or []:
            aid = (a.get("author") or {}).get("id")
            if aid:
                author_ids.add(str(aid))

    metrics: dict[str, AuthorMetric] = {}

    def load_author(aid: str) -> None:
        metrics[aid] = fetch_author_metric(aid, mailto)

    with ThreadPoolExecutor(max_workers=max(1, max_author_workers)) as pool:
        futs = {pool.submit(load_author, aid): aid for aid in author_ids}
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as e:
                aid = futs[fut]
                logger.warning("OpenAlex author worker failed %s: %s", aid, e)
                metrics[aid] = AuthorMetric(display_name="Unknown", h_index=0)

    out: dict[str, str] = {}
    for art in articles:
        link = str(art.link)
        en = build_enrichment_for_work(link_to_work.get(link), metrics)
        out[link] = en.format_block() if en else ""
    return out
