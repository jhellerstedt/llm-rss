"""Map article and RSS URLs to stable venue keys for journal suggestions."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from zulip_context import domain_from_url

# Nature article-type codes (prefix of /articles/sNNNNN-...) -> (venue slug, display name, .rss stem)
# Slug matches www.nature.com/{slug}.rss where applicable.
NATURE_ARTICLE_PREFIX: dict[str, tuple[str, str]] = {
    "s41586": ("nature", "Nature"),
    "s41567": ("nphys", "Nature Physics"),
    "s41467": ("ncomms", "Nature Communications"),
    "s41563": ("nmat", "Nature Materials"),
    "s41565": ("nenergy", "Nature Energy"),
    "s41534": ("npjqi", "npj Quantum Information"),
    "s41536": ("npjqi", "npj Quantum Information"),
    "s41598": ("srep", "Scientific Reports"),
    "s42003": ("ncb", "Communications Biology"),
    "s42004": ("cchem", "Communications Chemistry"),
    "s42005": ("cmat", "Communications Materials"),
    "s43247": ("cenv", "Communications Earth & Environment"),
    "s42254": ("natrevphys", "Nature Reviews Physics"),
}

# APS DOI fragment (after 10.1103/) -> feeds.aps.org slug
APS_DOI_JOURNAL_TO_SLUG: dict[str, str] = {
    "PhysRevLett": "prl",
    "PhysRevX": "prx",
    "PhysRevB": "prb",
    "PhysRevA": "pra",
    "PhysRevC": "prc",
    "PhysRevD": "prd",
    "PhysRevE": "pre",
    "PhysRevApplied": "prapplied",
    "PhysRevResearch": "prresearch",
    "PhysRevAccelBeams": "prab",
    "PhysRevPhysEducRes": "prper",
    "PhysRevLett.educ": "prl",  # rare
    "RevModPhys": "rmp",
    "PhysRev": "pr",  # legacy
    "PhysRevSTAB": "prab",
    "PhysRevSTPER": "prper",
}

APS_SLUG_DISPLAY: dict[str, str] = {
    "prl": "Physical Review Letters",
    "prx": "Physical Review X",
    "prb": "Physical Review B",
    "pra": "Physical Review A",
    "prc": "Physical Review C",
    "prd": "Physical Review D",
    "pre": "Physical Review E",
    "prapplied": "Physical Review Applied",
    "prresearch": "Physical Review Research",
    "prab": "Physical Review Accelerators and Beams",
    "prper": "Physical Review Physics Education Research",
    "rmp": "Reviews of Modern Physics",
    "pr": "Physical Review (legacy)",
}

_NATURE_ARTICLES_RE = re.compile(
    r"(?i)https?://(?:www\.)?nature\.com/articles/(s\d{5})[-\w]",
)
_NATURE_RSS_STEM_RE = re.compile(
    r"(?i)https?://(?:www\.)?nature\.com/([a-z0-9-]+)\.rss(?:$|[?#])",
)

_APS_DOI_RE = re.compile(
    r"(?i)https?://(?:link|journals)\.aps\.org/doi/(10\.1103/[^?\s#]+)",
)
_APS_FEED_SLUG_RE = re.compile(
    r"(?i)https?://feeds\.aps\.org/rss/recent/([a-z0-9]+)\.xml(?:$|[?#])",
)
_APS_JOURNALS_PREFIX_RE = re.compile(
    r"(?i)https?://journals\.aps\.org/([a-z0-9]+)/",
)

_IOP_DOI_IN_PATH_RE = re.compile(
    r"(?i)https?://(?:www\.)?iopscience\.iop\.org/article/10\.1088/([^/\s?#]+)",
)
_IOP_JOURNAL_PAGE_RE = re.compile(
    r"(?i)https?://(?:www\.)?iopscience\.iop\.org/journal/([^/\s?#]+)",
)


@dataclass(frozen=True)
class VenueRef:
    """A publisher sub-venue (journal / feed identity)."""

    venue_key: str
    display_name: str
    apex_domain: str
    suggested_rss: str | None
    journal_page_url: str | None = None


def _nature_rss_url(stem: str) -> str:
    return f"https://www.nature.com/{stem}.rss"


def venue_from_nature_article_url(url: str) -> VenueRef | None:
    m = _NATURE_ARTICLES_RE.search(url)
    if not m:
        return None
    prefix = m.group(1).lower()
    mapped = NATURE_ARTICLE_PREFIX.get(prefix)
    if mapped:
        stem, name = mapped
        return VenueRef(
            venue_key=f"nature:{stem}",
            display_name=name,
            apex_domain="nature.com",
            suggested_rss=_nature_rss_url(stem),
        )
    return VenueRef(
        venue_key=f"nature:article_{prefix}",
        display_name=f"Nature (article id {prefix})",
        apex_domain="nature.com",
        suggested_rss=None,
        journal_page_url=f"https://www.nature.com/articles/",
    )


def venue_from_nature_path_or_rss(url: str) -> VenueRef | None:
    """Match nature.com/<journal>/... or .rss feeds."""
    m = _NATURE_RSS_STEM_RE.search(url.strip())
    if m:
        stem = m.group(1).lower()
        if stem == "articles":
            return None
        return VenueRef(
            venue_key=f"nature:{stem}",
            display_name=f"Nature ({stem})",
            apex_domain="nature.com",
            suggested_rss=_nature_rss_url(stem),
        )
    p = urlparse(url.strip())
    host = (p.netloc or "").lower().removeprefix("www.")
    if host != "nature.com" or not p.path:
        return None
    parts = [x for x in p.path.split("/") if x]
    if not parts or parts[0] == "articles":
        return None
    stem = parts[0].lower()
    if stem.endswith(".rss"):
        stem = stem[:-4]
    if not stem or not stem.replace("-", "").isalnum():
        return None
    return VenueRef(
        venue_key=f"nature:{stem}",
        display_name=f"Nature ({stem})",
        apex_domain="nature.com",
        suggested_rss=_nature_rss_url(stem),
    )


def _aps_ref_from_slug(slug: str) -> VenueRef:
    slug = slug.lower()
    label = APS_SLUG_DISPLAY.get(slug, f"APS ({slug})")
    return VenueRef(
        venue_key=f"aps:{slug}",
        display_name=label,
        apex_domain="link.aps.org",
        suggested_rss=f"http://feeds.aps.org/rss/recent/{slug}.xml",
    )


def venue_from_aps_doi_url(url: str) -> VenueRef | None:
    m = _APS_DOI_RE.search(url)
    if not m:
        return None
    doi_tail = unquote(m.group(1))
    # 10.1103/PhysRevLett.130.010701 -> PhysRevLett
    rest = doi_tail.split("/", 1)[-1]
    journal_key = rest.split(".", 1)[0]
    slug = APS_DOI_JOURNAL_TO_SLUG.get(journal_key)
    if not slug:
        return VenueRef(
            venue_key=f"aps:{journal_key}",
            display_name=f"APS ({journal_key})",
            apex_domain="link.aps.org",
            suggested_rss=None,
        )
    return _aps_ref_from_slug(slug)


def venue_from_aps_feed_url(url: str) -> VenueRef | None:
    m = _APS_FEED_SLUG_RE.search(url.strip())
    if not m:
        return None
    slug = m.group(1).lower()
    ref = _aps_ref_from_slug(slug)
    return VenueRef(
        venue_key=ref.venue_key,
        display_name=ref.display_name,
        apex_domain="feeds.aps.org",
        suggested_rss=ref.suggested_rss,
    )


def venue_from_iop_url(url: str) -> VenueRef | None:
    m = _IOP_DOI_IN_PATH_RE.search(url) or _IOP_JOURNAL_PAGE_RE.search(url)
    if not m:
        return None
    journal_id = m.group(1)
    page = f"https://iopscience.iop.org/journal/{journal_id}"
    return VenueRef(
        venue_key=f"iop:{journal_id}",
        display_name=f"IOP journal {journal_id}",
        apex_domain="iopscience.iop.org",
        suggested_rss=None,
        journal_page_url=page,
    )


def venue_from_article_url(url: str) -> VenueRef | None:
    """Best-effort venue for a single HTTP(S) URL."""
    u = (url or "").strip()
    if not u:
        return None

    v = venue_from_nature_article_url(u)
    if v:
        return v
    v = venue_from_nature_path_or_rss(u)
    if v:
        return v
    v = venue_from_aps_doi_url(u)
    if v:
        return v
    v = venue_from_aps_feed_url(u)
    if v:
        return v
    v = venue_from_iop_url(u)
    if v:
        return v

    jm = _APS_JOURNALS_PREFIX_RE.search(u)
    if jm:
        slug = jm.group(1).lower()
        return _aps_ref_from_slug(slug)

    return None


def venue_from_feed_url(url: str) -> VenueRef | None:
    """Venue covered by an RSS feed URL in config `urls`."""
    u = (url or "").strip()
    if not u:
        return None

    v = venue_from_nature_path_or_rss(u)
    if v:
        return v
    v = venue_from_aps_feed_url(u)
    if v:
        return v

    j = _IOP_JOURNAL_PAGE_RE.search(u)
    if j:
        jid = j.group(1)
        return VenueRef(
            venue_key=f"iop:{jid}",
            display_name=f"IOP journal {jid}",
            apex_domain="iopscience.iop.org",
            suggested_rss=None,
            journal_page_url=f"https://iopscience.iop.org/journal/{jid}",
        )

    # Generic IOP RSS if path contains article/xml_feed or similar (best effort)
    if "iopscience.iop.org" in u.lower():
        p = urlparse(u)
        for part in p.path.split("/"):
            if re.fullmatch(r"\d{4}-\d{4}", part) or re.fullmatch(r"\d{7}", part):
                return VenueRef(
                    venue_key=f"iop:{part}",
                    display_name=f"IOP journal {part}",
                    apex_domain="iopscience.iop.org",
                    suggested_rss=None,
                    journal_page_url=f"https://iopscience.iop.org/journal/{part}",
                )

    return None


def tracked_venues_from_group_urls(urls: list[str]) -> set[str]:
    keys: set[str] = set()
    for raw in urls:
        u = str(raw).strip()
        v = venue_from_feed_url(u)
        if v:
            keys.add(v.venue_key)
            continue
        # Fallback: whole-host bucket for feeds we do not parse (e.g. cell.com)
        d = domain_from_url(u)
        if d:
            keys.add(f"host:{d}")
    return keys


def venue_fallback_host(url: str, domain: str) -> VenueRef:
    d = domain.lower().removeprefix("www.")
    return VenueRef(
        venue_key=f"host:{d}",
        display_name=d,
        apex_domain=d,
        suggested_rss=None,
    )


@dataclass
class VenueBucket:
    count: int
    display_name: str
    suggested_rss: str | None
    journal_page_url: str | None
    apex_domain: str
    example_url: str | None = None


def merge_bucket(dst: VenueBucket, src: VenueBucket) -> None:
    dst.count += src.count
    if not dst.example_url and src.example_url:
        dst.example_url = src.example_url
    if dst.suggested_rss is None and src.suggested_rss:
        dst.suggested_rss = src.suggested_rss
    if dst.journal_page_url is None and src.journal_page_url:
        dst.journal_page_url = src.journal_page_url
    # Prefer longer / more specific display name
    if len(src.display_name) > len(dst.display_name):
        dst.display_name = src.display_name


def bucket_from_ref(ref: VenueRef, example_url: str | None) -> VenueBucket:
    return VenueBucket(
        count=0,
        display_name=ref.display_name,
        suggested_rss=ref.suggested_rss,
        journal_page_url=ref.journal_page_url,
        apex_domain=ref.apex_domain,
        example_url=example_url,
    )
