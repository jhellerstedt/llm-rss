"""Resolve paper metadata from OpenAlex (h-index, first/last affiliations)."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
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
_AUTHOR_CACHE_VERSION = 2
# OpenAlex often merges distinct people who share a name; those profiles list
# many last-known institutions. h-index from such records is not usable.
_MAX_TRUSTED_LAST_KNOWN_INSTITUTIONS = 5
_AUTHOR_CACHE_TTL_SECONDS = 7 * 86400
_DEFAULT_REQUESTS_PER_SECOND = 8.0
_DEFAULT_MAX_AUTHOR_WORKERS = 2
_DEFAULT_MAX_AUTHOR_FETCHES_PER_RUN = 80
_OPENALEX_MAX_RETRIES = 5


class _OpenAlexRateLimiter:
    """Serialize OpenAlex HTTP pacing across worker threads."""

    def __init__(self, requests_per_second: float) -> None:
        rps = max(0.1, float(requests_per_second))
        self._interval = 1.0 / rps
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_allowed = now + self._interval


_rate_limiter = _OpenAlexRateLimiter(_DEFAULT_REQUESTS_PER_SECOND)

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
# Nature article URLs omit the 10.1038/ prefix (e.g. /articles/s41586-026-10638-w).
_NATURE_ARTICLE = re.compile(
    r"nature\.com/(?:articles|news)/([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


@dataclass(frozen=True)
class AuthorMetric:
    display_name: str
    #: Bibliometric h-index when known; ``None`` when missing or not credible.
    h_index: int | None
    #: OpenAlex ``last_known_institutions`` display names (empty if unknown).
    institutions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperEnrichment:
    top_author_name: str
    first_affiliation: str
    last_affiliation: str
    #: ``0`` is a real h-index; ``None`` means unknown / not available (display ``n/a``).
    top_h_index: int | None = None
    top_author_affiliation: str = "Unknown"
    #: From OpenAlex ``len(authorships)`` when a work is resolved; ``None`` if unknown.
    author_count: int | None = None
    #: arXiv abs URL when a preprint is found; prefer this over paywalled journal links.
    arxiv_url: str | None = None

    def format_block(self) -> str:
        h_label = _h_index_display(self.top_h_index)
        lines = [
            f"Highest h-index author on this paper: {self.top_author_name} "
            f"(h-index {h_label})",
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


def _norm_institution(s: str) -> str:
    t = str(s).strip().lower()
    t = re.sub(r"^the\s+", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _institutions_overlap(paper: list[str], author: tuple[str, ...]) -> bool:
    """True when we cannot check, or at least one institution name agrees."""
    pn = [_norm_institution(x) for x in paper if str(x).strip()]
    an = [_norm_institution(x) for x in author if str(x).strip()]
    if not pn or not an:
        return True
    for p in pn:
        for a in an:
            if p == a:
                return True
            if len(p) >= 8 and len(a) >= 8 and (p in a or a in p):
                return True
    return False


def _author_metric_trusted(m: AuthorMetric, paper_insts: list[str]) -> bool:
    if len(m.institutions) > _MAX_TRUSTED_LAST_KNOWN_INSTITUTIONS:
        return False
    return _institutions_overlap(paper_insts, m.institutions)


def _paper_institutions(a: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for inst in a.get("institutions") or []:
        if not isinstance(inst, dict):
            continue
        dn = str(inst.get("display_name") or "").strip()
        key = _norm_institution(dn)
        if dn and key not in seen:
            seen.add(key)
            out.append(dn)
    for aff in a.get("affiliations") or []:
        if not isinstance(aff, dict):
            continue
        raw = str(aff.get("raw_affiliation_string") or "").strip()
        key = _norm_institution(raw)
        if raw and key not in seen:
            seen.add(key)
            out.append(raw)
    return out


def _affiliation_matching_author(
    authorship: dict[str, Any], metric: AuthorMetric
) -> str:
    paper_insts = _paper_institutions(authorship)
    for inst in paper_insts:
        if _institutions_overlap([inst], metric.institutions):
            return inst
    return affiliation_for_authorship(authorship)


def _norm_person_name(name: str) -> str:
    """Lowercase + collapsed whitespace for comparing author strings across sources."""
    t = str(name).strip().lower()
    return re.sub(r"\s+", " ", t)


def _h_index_display(h: int | None) -> str:
    return "n/a" if h is None else str(h)


def _h_index_rank(h: int | None) -> int:
    """Sort key for picking the author with the highest h-index (missing below any real value)."""
    return -1 if h is None else h


def _plausible_author_h_index(h: int) -> int | None:
    """Reject obviously wrong h-index values (LLM / source confusion with citations)."""
    if h < 0:
        return None
    if h == 0:
        return 0
    if h > _MAX_PLAUSIBLE_AUTHOR_H_INDEX:
        logger.warning(
            "Ignoring implausible author h-index %d (cap %d)",
            h,
            _MAX_PLAUSIBLE_AUTHOR_H_INDEX,
        )
        return None
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
    if en.top_h_index is not None and en.top_h_index > 0:
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
            (top_h is None or top_h == 0)
            and not _is_unknown(kagi.top_author_name)
            and _norm_person_name(top_name) == _norm_person_name(kagi.top_author_name)
            and kagi.top_h_index is not None
            and (kagi.top_h_index > 0 or top_h is None)
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
    ac = (
        openalex.author_count
        if openalex.author_count is not None
        else kagi.author_count
    )
    return PaperEnrichment(
        top_author_name=top_name,
        first_affiliation=first,
        last_affiliation=last,
        top_h_index=top_h,
        top_author_affiliation=top_aff,
        author_count=ac,
        arxiv_url=openalex.arxiv_url or kagi.arxiv_url,
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
    if not _is_unknown(en.top_author_name) or (
        en.top_h_index is not None and en.top_h_index > 0
    ):
        name = en.top_author_name if not _is_unknown(en.top_author_name) else "Unknown"
        lines.append(
            f"Highest h-index author: {name} (h-index {_h_index_display(en.top_h_index)})"
        )
    if not _is_unknown(en.top_author_affiliation):
        lines.append(f"That author's affiliation: {en.top_author_affiliation}")
    return "\n".join(lines).strip()


def preferred_public_link(original: str, en: PaperEnrichment | None) -> str:
    """Prefer an arXiv abs URL over a paywalled journal landing page."""
    if en is not None and en.arxiv_url:
        return str(en.arxiv_url).strip() or original
    return original


class _KagiMetadataJson(BaseModel):
    top_author_name: str = Field(default="Unknown")
    top_author_h_index: int | None = Field(default=None)
    top_author_institution: str = Field(default="Unknown")
    first_author_institution: str = Field(default="Unknown")
    last_author_institution: str = Field(default="Unknown")

    @field_validator("top_author_h_index", mode="after")
    @classmethod
    def _cap_h_index(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v < 0:
            return None
        return _plausible_author_h_index(v)


class _KagiBatchPaperItem(BaseModel):
    """One row in a batched FastGPT metadata response."""

    paper_id: str = Field(..., min_length=1)
    top_author_name: str = Field(default="Unknown")
    top_author_h_index: int | None = Field(default=None)
    top_author_institution: str = Field(default="Unknown")
    first_author_institution: str = Field(default="Unknown")
    last_author_institution: str = Field(default="Unknown")

    @field_validator("top_author_h_index", mode="after")
    @classmethod
    def _cap_h_index(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v < 0:
            return None
        return _plausible_author_h_index(v)


# OpenAlex gaps are filled in chunked Kagi calls (one FastGPT invocation per chunk).
METADATA_KAGI_BATCH_MAX = 12
_METADATA_ABSTRACT_CHARS_PER_PAPER = 1800


def _enrichment_from_kagi_metadata_json(m: _KagiMetadataJson) -> PaperEnrichment:
    return PaperEnrichment(
        top_author_name=m.top_author_name.strip() or "Unknown",
        first_affiliation=m.first_author_institution.strip() or "Unknown",
        last_affiliation=m.last_author_institution.strip() or "Unknown",
        top_h_index=m.top_author_h_index,
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
- "top_author_h_index": integer >= 0 when known (use ``0`` only for a verified bibliometric h-index of zero), or JSON ``null`` when the name is Unknown or the h-index cannot be verified. This must be the bibliometric **h-index** (Hirsch index: h papers with at least h citations each), NOT total citations, NOT i10-index, NOT publication count. Typical values are under 150 even for very prominent researchers.
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
    if m:
        return m.group(1).rstrip(".,;)")
    nm = _NATURE_ARTICLE.search(raw)
    if nm:
        article_id = nm.group(1).rstrip(".,;)")
        if article_id:
            return f"10.1038/{article_id}"
    return None


def extract_arxiv_id(link: str) -> str | None:
    m = _ARXIV_NEW.search(link)
    if m:
        return m.group("id")
    m = _ARXIV_DATACITE.search(link)
    return m.group("id") if m else None


def _arxiv_abs_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/abs/{arxiv_id}"


def arxiv_url_from_work(work: dict[str, Any] | None) -> str | None:
    """Return an arXiv abs URL from OpenAlex locations / ids, if present."""
    if not work:
        return None
    candidates: list[str] = []
    ids = work.get("ids")
    if isinstance(ids, dict):
        for v in ids.values():
            if isinstance(v, str):
                candidates.append(v)
    doi = work.get("doi")
    if isinstance(doi, str):
        candidates.append(doi)
    oa = work.get("open_access")
    if isinstance(oa, dict):
        ou = oa.get("oa_url")
        if isinstance(ou, str):
            candidates.append(ou)
    locs: list[Any] = []
    primary = work.get("primary_location")
    if primary:
        locs.append(primary)
    locs.extend(work.get("locations") or [])
    for loc in locs:
        if not isinstance(loc, dict):
            continue
        for key in ("landing_page_url", "pdf_url"):
            u = loc.get(key)
            if isinstance(u, str):
                candidates.append(u)
    for c in candidates:
        aid = extract_arxiv_id(c)
        if aid:
            return _arxiv_abs_url(aid)
    return None


def search_arxiv_by_title(title: str) -> str | None:
    """Best-effort arXiv API lookup by title; None on miss or error."""
    q = title.strip()
    if not q:
        return None
    try:
        r = requests.get(
            "https://export.arxiv.org/api/query",
            params={"search_query": f'ti:"{q}"', "max_results": 5},
            timeout=12,
            headers=_HTTP_HEADERS,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        logger.info("arXiv title search failed for %r: %s", q[:80], e)
        return None
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        logger.debug("arXiv title search: XML parse failed for %r", q[:80])
        return None
    for entry in root.findall("atom:entry", _ATOM_NS):
        et = entry.findtext("atom:title", default="", namespaces=_ATOM_NS) or ""
        et = re.sub(r"\s+", " ", et).strip()
        if not _titles_match(q, et):
            continue
        eid = entry.findtext("atom:id", default="", namespaces=_ATOM_NS) or ""
        aid = extract_arxiv_id(eid)
        if aid:
            return _arxiv_abs_url(aid)
    return None


def _work_is_closed_access(work: dict[str, Any] | None) -> bool:
    if not work:
        return True
    oa = work.get("open_access")
    if isinstance(oa, dict) and oa.get("is_oa") is True:
        return False
    return True


def enrichment_with_arxiv(
    en: PaperEnrichment | None,
    work: dict[str, Any] | None,
    article: ArticleInfo,
) -> PaperEnrichment | None:
    """Attach an arXiv abs URL when the journal link is closed-access or already arXiv."""
    url: str | None = None
    aid = extract_arxiv_id(str(article.link))
    if aid:
        url = _arxiv_abs_url(aid)
    if not url:
        url = arxiv_url_from_work(work)
    if not url and _work_is_closed_access(work):
        url = search_arxiv_by_title(article.title)
    if not url:
        return en
    if en is None:
        return PaperEnrichment(
            top_author_name="Unknown",
            first_affiliation="Unknown",
            last_affiliation="Unknown",
            arxiv_url=url,
        )
    return replace(en, arxiv_url=url)


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


def _author_cache_key(author_openalex_id_url: str) -> str:
    return author_openalex_id_url.rstrip("/").split("/")[-1]


def _load_author_metric_cache(
    path: Path | None,
    *,
    ttl_seconds: int = _AUTHOR_CACHE_TTL_SECONDS,
) -> dict[str, AuthorMetric]:
    if path is None or not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.debug("OpenAlex author cache unreadable at %s; starting fresh", path)
        return {}
    if not isinstance(doc, dict):
        return {}
    if doc.get("version") != _AUTHOR_CACHE_VERSION:
        return {}
    authors = doc.get("authors")
    if not isinstance(authors, dict):
        return {}
    now = time.time()
    out: dict[str, AuthorMetric] = {}
    for key, row in authors.items():
        if not isinstance(row, dict):
            continue
        try:
            cached_at = float(row.get("cached_at") or 0)
        except (TypeError, ValueError):
            continue
        if cached_at <= 0 or (now - cached_at) > ttl_seconds:
            continue
        raw_insts = row.get("institutions")
        if not isinstance(raw_insts, list):
            continue
        insts = tuple(str(x).strip() for x in raw_insts if str(x).strip())
        raw_h = row.get("h_index")
        h: int | None
        if raw_h is None:
            h = None
        else:
            try:
                h = int(raw_h)
            except (TypeError, ValueError):
                h = None
        out[str(key)] = AuthorMetric(
            display_name=str(row.get("display_name") or "Unknown"),
            h_index=h,
            institutions=insts,
        )
    return out


def _save_author_metric_cache(
    path: Path | None,
    metrics: dict[str, AuthorMetric],
    *,
    prior: dict[str, AuthorMetric] | None = None,
) -> None:
    if path is None:
        return
    merged = dict(prior or {})
    merged.update(metrics)
    now = time.time()
    authors: dict[str, dict[str, Any]] = {}
    for key, metric in merged.items():
        authors[key] = {
            "display_name": metric.display_name,
            "h_index": metric.h_index,
            "institutions": list(metric.institutions),
            "cached_at": now,
        }
    doc = {"version": _AUTHOR_CACHE_VERSION, "authors": authors}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("OpenAlex author cache write failed %s: %s", path, e)


def configure_openalex_rate_limit(requests_per_second: float) -> None:
    global _rate_limiter
    _rate_limiter = _OpenAlexRateLimiter(requests_per_second)


def _get_json(
    url: str,
    mailto: str,
    *,
    params: dict[str, str | int] | None = None,
    max_retries: int = _OPENALEX_MAX_RETRIES,
) -> Any | None:
    merged: dict[str, str | int] = dict(params or {})
    if mailto:
        merged["mailto"] = mailto
    last_error: Exception | None = None
    for attempt in range(max(1, max_retries)):
        _rate_limiter.wait()
        try:
            record_openalex_http(1)
            r = requests.get(
                url, params=merged, timeout=25, headers=_HTTP_HEADERS
            )
            if r.status_code == 404:
                logger.debug(
                    "OpenAlex not found (404): %s", url.split("?", 1)[0]
                )
                return None
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                try:
                    delay = max(1, int(retry_after)) if retry_after else 0
                except (TypeError, ValueError):
                    delay = 0
                if delay <= 0:
                    delay = min(2**attempt, 30)
                logger.info(
                    "OpenAlex rate limited (429) on %s; retry in %ss (attempt %d/%d)",
                    url.split("?", 1)[0],
                    delay,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(delay)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_error = e
            if attempt + 1 < max_retries:
                time.sleep(min(2**attempt, 10))
                continue
    if last_error is not None:
        logger.warning("OpenAlex request failed %s: %s", url, last_error)
    return None


def fetch_work(article: ArticleInfo, mailto: str) -> Any | None:
    for direct in direct_openalex_work_urls(str(article.link)):
        data = _get_json(direct, mailto)
        if data and data.get("id"):
            return data

    title = article.title.strip()
    if not title:
        return None
    payload = _get_json(
        f"{OPENALEX_BASE}/works",
        mailto,
        params={"search": title, "per_page": 5},
    )
    if not isinstance(payload, dict):
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


def _institutions_from_author_payload(data: dict[str, Any]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for inst in data.get("last_known_institutions") or []:
        if not isinstance(inst, dict):
            continue
        dn = str(inst.get("display_name") or "").strip()
        key = _norm_institution(dn)
        if not dn or key in seen:
            continue
        seen.add(key)
        out.append(dn)
    return tuple(out)


def fetch_author_metric(
    author_openalex_id_url: str,
    mailto: str,
    *,
    cache: dict[str, AuthorMetric] | None = None,
) -> AuthorMetric:
    key = _author_cache_key(author_openalex_id_url)
    if cache is not None and key in cache:
        return cache[key]
    url = _authors_api_path(author_openalex_id_url)
    data = _get_json(url, mailto)
    if not data:
        metric = AuthorMetric(display_name="", h_index=None)
    else:
        name = str(data.get("display_name") or "").strip()
        stats = data.get("summary_stats") or {}
        h: int | None
        if "h_index" not in stats:
            h = None
        else:
            raw = stats.get("h_index")
            if raw is None:
                h = None
            else:
                try:
                    h = int(raw)
                except (TypeError, ValueError):
                    h = None
        plausible: int | None = None if h is None else _plausible_author_h_index(h)
        insts = _institutions_from_author_payload(data)
        metric = AuthorMetric(
            display_name=name or "Unknown",
            h_index=plausible,
            institutions=insts,
        )
    if cache is not None:
        cache[key] = metric
    return metric


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
    best_metric = AuthorMetric(display_name="Unknown", h_index=None)

    for idx, a in enumerate(authorships):
        author = a.get("author") or {}
        aid = author.get("id")
        if not aid:
            continue
        aid = str(aid)
        m = metrics_by_author_url.get(
            aid, AuthorMetric(display_name="Unknown", h_index=None)
        )
        paper_insts = _paper_institutions(a)
        if not _author_metric_trusted(m, paper_insts):
            logger.info(
                "OpenAlex: skipping untrusted author profile %s (%s, h=%s, %d institutions)",
                aid,
                m.display_name or "Unknown",
                m.h_index,
                len(m.institutions),
            )
            continue
        if best_idx is None:
            best_metric = m
            best_idx = idx
        elif _h_index_rank(m.h_index) > _h_index_rank(best_metric.h_index):
            best_metric = m
            best_idx = idx
        elif (
            _h_index_rank(m.h_index) == _h_index_rank(best_metric.h_index)
            and best_idx is not None
            and idx < best_idx
        ):
            best_metric = m
            best_idx = idx

    if best_idx is None:
        top_name = "Unknown"
        top_h = None
        top_aff = "Unknown"
    else:
        top_name = best_metric.display_name
        top_h = best_metric.h_index
        top_aff = _affiliation_matching_author(authorships[best_idx], best_metric)

    first_a, last_a = _first_last_authorships(authorships)
    first_aff = affiliation_for_authorship(first_a) if first_a else "Unknown"
    last_aff = affiliation_for_authorship(last_a) if last_a else "Unknown"

    return PaperEnrichment(
        top_author_name=top_name,
        first_affiliation=first_aff,
        last_affiliation=last_aff,
        top_h_index=top_h,
        top_author_affiliation=top_aff,
        author_count=len(authorships),
    )


def batch_enrich_articles(
    articles: list[ArticleInfo],
    mailto: str,
    max_work_workers: int = 3,
    max_author_workers: int = _DEFAULT_MAX_AUTHOR_WORKERS,
    *,
    max_author_fetches_per_run: int = _DEFAULT_MAX_AUTHOR_FETCHES_PER_RUN,
    author_cache_path: Path | str | None = None,
    requests_per_second: float = _DEFAULT_REQUESTS_PER_SECOND,
) -> dict[str, PaperEnrichment | None]:
    """Map article link -> structured metadata from OpenAlex (None if work not resolved)."""
    if not articles:
        return {}

    configure_openalex_rate_limit(requests_per_second)
    cache_file = Path(author_cache_path) if author_cache_path else None
    author_cache = _load_author_metric_cache(cache_file)

    link_to_work: dict[str, dict[str, Any] | None] = {}

    def load_work(art: ArticleInfo) -> None:
        link_to_work[str(art.link)] = fetch_work(art, mailto)

    with ThreadPoolExecutor(max_workers=max(1, max_work_workers)) as pool:
        futs = [pool.submit(load_work, art) for art in articles]
        for f in futs:
            f.result()

    author_ids: list[str] = []
    seen_author_ids: set[str] = set()
    for w in link_to_work.values():
        if not w:
            continue
        for a in w.get("authorships") or []:
            aid = (a.get("author") or {}).get("id")
            if not aid:
                continue
            aid_s = str(aid)
            if aid_s in seen_author_ids:
                continue
            seen_author_ids.add(aid_s)
            author_ids.append(aid_s)

    to_fetch = [
        aid
        for aid in author_ids
        if _author_cache_key(aid) not in author_cache
    ]
    cap = max(0, int(max_author_fetches_per_run))
    if cap and len(to_fetch) > cap:
        logger.info(
            "OpenAlex: capping author fetches to %d (%d unique authors, %d cached)",
            cap,
            len(author_ids),
            len(author_ids) - len(to_fetch),
        )
        to_fetch = to_fetch[:cap]

    metrics: dict[str, AuthorMetric] = {}
    for aid in author_ids:
        key = _author_cache_key(aid)
        if key in author_cache:
            metrics[aid] = author_cache[key]

    def load_author(aid: str) -> None:
        metrics[aid] = fetch_author_metric(aid, mailto, cache=author_cache)

    if to_fetch:
        with ThreadPoolExecutor(max_workers=max(1, max_author_workers)) as pool:
            futs = {pool.submit(load_author, aid): aid for aid in to_fetch}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    aid = futs[fut]
                    logger.warning("OpenAlex author worker failed %s: %s", aid, e)
                    metrics[aid] = AuthorMetric(display_name="Unknown", h_index=None)

    for aid in author_ids:
        if aid not in metrics:
            metrics[aid] = author_cache.get(
                _author_cache_key(aid),
                AuthorMetric(display_name="Unknown", h_index=None),
            )

    if cache_file is not None and to_fetch:
        _save_author_metric_cache(cache_file, author_cache)

    out: dict[str, PaperEnrichment | None] = {}
    for art in articles:
        link = str(art.link)
        en = build_enrichment_for_work(link_to_work.get(link), metrics)
        out[link] = enrichment_with_arxiv(en, link_to_work.get(link), art)
    return out
