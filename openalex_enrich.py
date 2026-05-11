"""Resolve paper metadata from OpenAlex (h-index, first/last affiliations)."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote

import requests
from pydantic import BaseModel, Field, ValidationError, field_validator

from adapter import ArticleInfo
from api_usage import record_openalex_http
from fastgpt_reply import try_load_json_object_from_llm
from kagi_quota import KagiOpenAlexFallbackQuotaExceeded, KagiSessionQuotaExceeded

if TYPE_CHECKING:
    from kagi_client import KagiClient

logger = logging.getLogger(__name__)

# Individual h-index above this is not credible (models often substitute citation
# totals, i10-index, or other counts). Real-world scholar h-indices stay far below.
_MAX_PLAUSIBLE_AUTHOR_H_INDEX = 400

OPENALEX_BASE = "https://api.openalex.org"
_HTTP_HEADERS = {"User-Agent": "llm-rss/openalex-enrich"}

_ARXIV_NEW = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(?P<id>\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
# arXiv DOIs minted by DataCite (common in RSS); OpenAlex often lacks /works/doi/... for new IDs.
_ARXIV_DATACITE = re.compile(
    r"(?:doi\.org/)?10\.48550/arXiv\.(?P<id>\d{4}\.\d{4,5})(?:v\d+)?",
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
    top_author_affiliation: str = "Unknown"

    def format_block(self) -> str:
        lines = [
            f"Highest h-index author on this paper: {self.top_author_name} "
            f"(h-index {self.top_h_index})",
        ]
        if not _is_unknown(self.top_author_affiliation):
            lines.append(f"That author's affiliation: {self.top_author_affiliation}")
        if self.first_affiliation == self.last_affiliation:
            lines.append(
                f"Institution (first & last author): {self.first_affiliation}"
            )
        else:
            lines.append(f"First author institution: {self.first_affiliation}")
            lines.append(f"Last author institution: {self.last_affiliation}")
        return "\n".join(lines)


def _is_unknown(s: str) -> bool:
    t = str(s).strip()
    return not t or t.lower() == "unknown"


def _norm_person_name(name: str) -> str:
    """Lowercase + collapsed whitespace for comparing author strings across sources."""
    t = str(name).strip().lower()
    return re.sub(r"\s+", " ", t)


def _plausible_author_h_index(h: int) -> int:
    """Drop obviously wrong h-index values (LLM / source confusion with citations)."""
    if h <= 0:
        return 0
    if h > _MAX_PLAUSIBLE_AUTHOR_H_INDEX:
        logger.warning(
            "Ignoring implausible author h-index %d (cap %d)",
            h,
            _MAX_PLAUSIBLE_AUTHOR_H_INDEX,
        )
        return 0
    return h


def paper_enrichment_incomplete(en: PaperEnrichment | None) -> bool:
    """True if OpenAlex (or prior step) left any field we try to backfill via Kagi."""
    if en is None:
        return True
    if _is_unknown(en.top_author_name):
        return True
    if _is_unknown(en.first_affiliation) or _is_unknown(en.last_affiliation):
        return True
    return False


def paper_enrichment_has_any_signal(en: PaperEnrichment | None) -> bool:
    if en is None:
        return False
    if not _is_unknown(en.top_author_name):
        return True
    if en.top_h_index > 0:
        return True
    if not _is_unknown(en.top_author_affiliation):
        return True
    if not _is_unknown(en.first_affiliation):
        return True
    if not _is_unknown(en.last_affiliation):
        return True
    return False


def merge_paper_enrichment(
    openalex: PaperEnrichment | None,
    kagi: PaperEnrichment | None,
) -> PaperEnrichment | None:
    """Prefer OpenAlex where it is usable; fill gaps from Kagi."""
    if kagi is None:
        return openalex
    if openalex is None:
        return kagi
    if _is_unknown(openalex.top_author_name):
        top_name = kagi.top_author_name
        top_h = kagi.top_h_index
    else:
        top_name = openalex.top_author_name
        top_h = openalex.top_h_index
        # OpenAlex often has the right author but h-index 0 (new profile / missing
        # stats). Kagi may have a verifiable h-index for the same person; only merge
        # h when names agree so we do not attach a senior co-author's h to someone else.
        if (
            top_h == 0
            and kagi.top_h_index > 0
            and not _is_unknown(kagi.top_author_name)
            and _norm_person_name(top_name) == _norm_person_name(kagi.top_author_name)
        ):
            top_h = kagi.top_h_index
    first = (
        openalex.first_affiliation
        if not _is_unknown(openalex.first_affiliation)
        else kagi.first_affiliation
    )
    last = (
        openalex.last_affiliation
        if not _is_unknown(openalex.last_affiliation)
        else kagi.last_affiliation
    )
    top_aff = (
        openalex.top_author_affiliation
        if not _is_unknown(openalex.top_author_affiliation)
        else kagi.top_author_affiliation
    )
    return PaperEnrichment(
        top_author_name=top_name,
        top_h_index=top_h,
        first_affiliation=first,
        last_affiliation=last,
        top_author_affiliation=top_aff,
    )


def format_enrichment_for_feed(en: PaperEnrichment | None) -> str:
    if en is None or not paper_enrichment_has_any_signal(en):
        return ""
    return en.format_block()


def format_enrichment_for_feedback_zulip(en: PaperEnrichment | None) -> str:
    """Short lines for Zulip feedback ranking (h-index author + affiliation when known)."""
    if en is None or not paper_enrichment_has_any_signal(en):
        return ""
    lines: list[str] = []
    if not _is_unknown(en.top_author_name) or en.top_h_index > 0:
        name = en.top_author_name if not _is_unknown(en.top_author_name) else "Unknown"
        lines.append(f"Highest h-index author: {name} (h-index {en.top_h_index})")
    if not _is_unknown(en.top_author_affiliation):
        lines.append(f"That author's affiliation: {en.top_author_affiliation}")
    return "\n".join(lines).strip()


class _KagiMetadataJson(BaseModel):
    top_author_name: str = Field(default="Unknown")
    top_author_h_index: int = Field(default=0, ge=0)
    top_author_institution: str = Field(default="Unknown")
    first_author_institution: str = Field(default="Unknown")
    last_author_institution: str = Field(default="Unknown")

    @field_validator("top_author_h_index", mode="after")
    @classmethod
    def _cap_h_index(cls, v: int) -> int:
        return _plausible_author_h_index(v)


class _KagiBatchPaperItem(BaseModel):
    """One row in a batched FastGPT metadata response."""

    paper_id: str = Field(..., min_length=1)
    top_author_name: str = Field(default="Unknown")
    top_author_h_index: int = Field(default=0, ge=0)
    top_author_institution: str = Field(default="Unknown")
    first_author_institution: str = Field(default="Unknown")
    last_author_institution: str = Field(default="Unknown")

    @field_validator("top_author_h_index", mode="after")
    @classmethod
    def _cap_h_index(cls, v: int) -> int:
        return _plausible_author_h_index(v)


# OpenAlex gaps are filled in chunked Kagi calls (one FastGPT invocation per chunk).
METADATA_KAGI_BATCH_MAX = 12
_METADATA_ABSTRACT_CHARS_PER_PAPER = 1800


def _enrichment_from_kagi_metadata_json(m: _KagiMetadataJson) -> PaperEnrichment:
    return PaperEnrichment(
        top_author_name=m.top_author_name.strip() or "Unknown",
        top_h_index=int(m.top_author_h_index),
        first_affiliation=m.first_author_institution.strip() or "Unknown",
        last_affiliation=m.last_author_institution.strip() or "Unknown",
        top_author_affiliation=m.top_author_institution.strip() or "Unknown",
    )


def _paper_block_for_kagi_batch(art: ArticleInfo) -> str:
    authors_line = (art.authors or "").strip() or "(not provided)"
    pid = str(art.link)
    abst = (art.abstract or "").strip()
    if len(abst) > _METADATA_ABSTRACT_CHARS_PER_PAPER:
        abst = abst[:_METADATA_ABSTRACT_CHARS_PER_PAPER] + "\n[truncated]"
    return (
        f"paper_id: {pid}\n"
        f"Title: {art.title}\n"
        f"Link: {art.link}\n"
        f"Abstract:\n{abst}\n"
        f"RSS author line (may be incomplete): {authors_line}\n"
    )


def fetch_metadata_batch_via_kagi(
    kagi: KagiClient, articles: list[ArticleInfo]
) -> dict[str, PaperEnrichment]:
    """One FastGPT call for many papers; map link -> enrichment (omits failures)."""
    if not articles:
        return {}
    expected = {str(a.link) for a in articles}
    blocks = [_paper_block_for_kagi_batch(a) for a in articles]
    joined = "\n---\n".join(blocks)
    n = len(articles)
    prompt = f"""You are extracting bibliometric metadata for {n} academic paper(s). Use web search if it helps.

Each paper below is separated by ---. The line paper_id identifies that paper; you MUST echo the same paper_id string in your JSON output for that paper.

{joined}

Respond with ONLY a single JSON object (no markdown code fences, no other text) with exactly one key:
"papers": array of {n} object(s) — one per paper above, in any order. Each object must have:
- "paper_id": string (exactly one of the paper_id values from above)
- "top_author_name": string (full name of the listed author with highest verifiable h-index; "Unknown" if unclear)
- "top_author_h_index": integer >= 0 (0 if name is Unknown or h-index unknown). This must be the bibliometric **h-index** (Hirsch index: h papers with at least h citations each), NOT total citations, NOT i10-index, NOT publication count. Typical values are under 150 even for very prominent researchers.
- "top_author_institution": string
- "first_author_institution": string
- "last_author_institution": string

If a paper has only one author, repeat the same institution in first and last author fields when appropriate. Apply rules independently per paper.
"""
    try:
        raw = kagi.fastgpt_query(prompt, openalex_fallback=True)
    except Exception as e:
        if isinstance(e, (KagiOpenAlexFallbackQuotaExceeded, KagiSessionQuotaExceeded)):
            logger.info(
                "Skipping Kagi metadata batch for %d paper(s): %s",
                n,
                e,
            )
            return {}
        logger.warning("Kagi metadata batch query failed (%d papers): %s", n, e)
        return {}
    data = try_load_json_object_from_llm(raw or "")
    if not data:
        logger.warning(
            "Kagi metadata batch JSON parse failed (%d papers); snippet=%s",
            n,
            (raw or "")[:500],
        )
        return {}
    papers_raw = data.get("papers")
    if papers_raw is None and isinstance(data, list):
        papers_raw = data
    if not isinstance(papers_raw, list):
        logger.warning(
            "Kagi metadata batch: expected papers array (%d papers); snippet=%s",
            n,
            (raw or "")[:400],
        )
        return {}
    out: dict[str, PaperEnrichment] = {}
    for i, item in enumerate(papers_raw):
        if not isinstance(item, dict):
            continue
        try:
            row = _KagiBatchPaperItem.model_validate(item)
        except ValidationError as e:
            logger.warning("Kagi metadata batch row %d invalid: %s", i, e)
            continue
        pid = str(row.paper_id).strip()
        if pid not in expected:
            logger.debug("Kagi metadata batch: unexpected paper_id %r", pid[:120])
            continue
        meta = _KagiMetadataJson(
            top_author_name=row.top_author_name,
            top_author_h_index=row.top_author_h_index,
            top_author_institution=row.top_author_institution,
            first_author_institution=row.first_author_institution,
            last_author_institution=row.last_author_institution,
        )
        out[pid] = _enrichment_from_kagi_metadata_json(meta)
    return out


def fetch_metadata_via_kagi(kagi: KagiClient, article: ArticleInfo) -> PaperEnrichment | None:
    """Single-paper convenience wrapper (one batched FastGPT call)."""
    got = fetch_metadata_batch_via_kagi(kagi, [article])
    return got.get(str(article.link))


def apply_kagi_metadata_backfill(
    by_link: dict[str, PaperEnrichment | None],
    articles: list[ArticleInfo],
    kagi: KagiClient,
) -> None:
    """Mutates by_link: runs Kagi when enrichment is incomplete (batched FastGPT calls)."""
    need: list[ArticleInfo] = []
    for art in articles:
        link = str(art.link)
        cur = by_link.get(link)
        if paper_enrichment_incomplete(cur):
            need.append(art)
    if not need:
        return
    batch_size = max(1, int(METADATA_KAGI_BATCH_MAX))
    for i in range(0, len(need), batch_size):
        batch = need[i : i + batch_size]
        got = fetch_metadata_batch_via_kagi(kagi, batch)
        for art in batch:
            link = str(art.link)
            cur = by_link.get(link)
            kg = got.get(link)
            by_link[link] = merge_paper_enrichment(cur, kg)


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
    if m:
        return m.group("id")
    m = _ARXIV_DATACITE.search(link)
    return m.group("id") if m else None


def direct_openalex_work_urls(link: str) -> list[str]:
    """Ordered /works/{url-encoded loc} URLs to try. DataCite arXiv DOIs are what OpenAlex indexes."""
    seen_locs: set[str] = set()
    out: list[str] = []

    def add_loc(loc: str) -> None:
        if loc in seen_locs:
            return
        seen_locs.add(loc)
        out.append(f"{OPENALEX_BASE}/works/{quote(loc, safe='')}")

    arxiv_id = extract_arxiv_id(link)
    if arxiv_id:
        add_loc(f"https://doi.org/10.48550/arXiv.{arxiv_id}")
        add_loc(f"https://arxiv.org/abs/{arxiv_id}")
    doi = extract_doi_from_link(link)
    if doi:
        add_loc(f"https://doi.org/{doi}")
    return out


def _authors_api_path(author_openalex_id_url: str) -> str:
    tail = author_openalex_id_url.rstrip("/").split("/")[-1]
    return f"{OPENALEX_BASE}/authors/{tail}"


def _get_json(url: str, mailto: str) -> Any | None:
    params: dict[str, str | int] = {}
    if mailto:
        params["mailto"] = mailto
    try:
        record_openalex_http(1)
        r = requests.get(
            url, params=params, timeout=25, headers=_HTTP_HEADERS
        )
        if r.status_code == 404:
            logger.debug(
                "OpenAlex not found (404): %s", url.split("?", 1)[0]
            )
            return None
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        logger.warning("OpenAlex request failed %s: %s", url, e)
        return None


def fetch_work(article: ArticleInfo, mailto: str) -> Any | None:
    for direct in direct_openalex_work_urls(str(article.link)):
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
        record_openalex_http(1)
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
    h = _plausible_author_h_index(max(0, h))
    return AuthorMetric(display_name=name or "Unknown", h_index=h)


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

    top_aff = "Unknown"
    if best_idx is not None and 0 <= best_idx < len(authorships):
        top_aff = affiliation_for_authorship(authorships[best_idx])

    return PaperEnrichment(
        top_author_name=top_name,
        top_h_index=top_h,
        first_affiliation=first_aff,
        last_affiliation=last_aff,
        top_author_affiliation=top_aff,
    )


def batch_enrich_articles(
    articles: list[ArticleInfo],
    mailto: str,
    max_work_workers: int = 3,
    max_author_workers: int = 6,
) -> dict[str, PaperEnrichment | None]:
    """Map article link -> structured metadata from OpenAlex (None if work not resolved)."""
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

    out: dict[str, PaperEnrichment | None] = {}
    for art in articles:
        link = str(art.link)
        en = build_enrichment_for_work(link_to_work.get(link), metrics)
        out[link] = en
    return out
