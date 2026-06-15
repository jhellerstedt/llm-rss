"""Resolve an ORCID id/URL or Google Scholar profile URL to a WhitelistedAuthor.

Used only when ADDING an author (low volume), so it may make a few HTTP calls and
fall back gracefully. Scan-time matching never calls this module.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, quote, urlparse

import requests
from bs4 import BeautifulSoup

from api_usage import record_openalex_http
from author_whitelist import WhitelistedAuthor, normalize_author_name

logger = logging.getLogger(__name__)

OPENALEX_BASE = "https://api.openalex.org"
_HEADERS = {"User-Agent": "llm-rss/author-resolve"}
_ORCID = re.compile(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", re.IGNORECASE)


class AuthorResolveError(Exception):
    """Raised when input cannot be resolved to an author."""


def parse_author_input(s: str) -> tuple[str, str]:
    """Return (kind, value): kind in {'orcid','scholar','unknown'}."""
    text = (s or "").strip()
    if "scholar.google." in text:
        qs = parse_qs(urlparse(text).query)
        user = (qs.get("user") or [""])[0]
        if user:
            return ("scholar", user)
    m = _ORCID.search(text)
    if m:
        return ("orcid", m.group(1).upper())
    return ("unknown", text)


def _get(url: str, *, timeout: int = 20, headers: dict | None = None):
    return requests.get(url, timeout=timeout, headers=headers or _HEADERS)


def fetch_orcid_person(orcid: str) -> tuple[str, list[str]]:
    """Return (display_name, aliases) from the ORCID public API; ('', []) on failure."""
    try:
        r = _get(
            f"https://pub.orcid.org/v3.0/{orcid}/person",
            headers={**_HEADERS, "Accept": "application/json"},
        )
        r.raise_for_status()
        data = r.json() or {}
    except Exception:
        logger.warning("ORCID person fetch failed for %s", orcid)
        return ("", [])
    name = data.get("name") or {}
    given = ((name.get("given-names") or {}) or {}).get("value", "")
    family = ((name.get("family-name") or {}) or {}).get("value", "")
    display = " ".join(p for p in (given, family) if p).strip()
    aliases: list[str] = []
    others = ((name.get("other-names") or {}) or {}).get("other-name") or []
    for o in others:
        content = (o or {}).get("content")
        if content:
            aliases.append(content)
    return (display, aliases)


def _author_from_openalex(
    oa: dict, *, source: str, extra_aliases: list[str] | tuple[str, ...] = ()
) -> WhitelistedAuthor:
    oa_url = oa.get("id") or ""
    short = oa_url.rsplit("/", 1)[-1] if oa_url else None
    orcid_url = oa.get("orcid") or None
    bare_orcid = None
    if orcid_url:
        m = _ORCID.search(orcid_url)
        bare_orcid = m.group(1).upper() if m else None
    display = oa.get("display_name") or ""
    aliases = list(oa.get("display_name_alternatives") or [])
    aliases.extend(extra_aliases)
    if display:
        aliases.append(display)
    insts = oa.get("last_known_institutions") or []
    affiliation = (insts[0] or {}).get("display_name") if insts else None
    if bare_orcid:
        ident = f"https://orcid.org/{bare_orcid}"
    elif short:
        ident = f"openalex:{short}"
    else:
        ident = f"name:{normalize_author_name(display)}"
    return WhitelistedAuthor(
        id=ident,
        display_name=display,
        name_aliases=sorted({a for a in aliases if a and a.strip()}),
        orcid=bare_orcid,
        openalex_id=short,
        affiliation=affiliation,
        works_count=oa.get("works_count"),
        source=source,
    )


def fetch_openalex_author_by_orcid(orcid: str, *, mailto: str | None) -> dict | None:
    url = f"{OPENALEX_BASE}/authors/orcid:{orcid}"
    if mailto:
        url += f"?mailto={mailto}"
    try:
        r = _get(url)
        record_openalex_http(1)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        logger.warning("OpenAlex author-by-orcid failed for %s", orcid)
        return None


def fetch_openalex_author_by_name(name: str, *, mailto: str | None) -> dict | None:
    url = f"{OPENALEX_BASE}/authors?search={quote(name)}"
    if mailto:
        url += f"&mailto={mailto}"
    try:
        r = _get(url)
        record_openalex_http(1)
        if r.status_code != 200:
            return None
        results = (r.json() or {}).get("results") or []
        return results[0] if results else None
    except Exception:
        logger.warning("OpenAlex author search failed for %s", name)
        return None


def fetch_scholar_profile(user_id: str) -> tuple[str, str | None]:
    """Best-effort scrape of a Scholar profile; raises AuthorResolveError on failure."""
    url = f"https://scholar.google.com/citations?user={user_id}&hl=en"
    try:
        r = _get(url, headers={**_HEADERS, "Accept-Language": "en"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        name_el = soup.find(id="gsc_prf_in")
        if not name_el or not name_el.get_text(strip=True):
            raise AuthorResolveError("scholar: could not read profile name")
        name = name_el.get_text(strip=True)
        aff_el = soup.find(class_="gsc_prf_il")
        affiliation = aff_el.get_text(strip=True) if aff_el else None
        return (name, affiliation)
    except AuthorResolveError:
        raise
    except Exception as e:
        raise AuthorResolveError(f"scholar: fetch failed ({e})")


def resolve(
    input_str: str, *, mailto: str | None, added_by: str | None = None
) -> WhitelistedAuthor:
    kind, value = parse_author_input(input_str)
    if kind == "orcid":
        display, orcid_aliases = fetch_orcid_person(value)
        oa = fetch_openalex_author_by_orcid(value, mailto=mailto)
        if oa:
            author = _author_from_openalex(
                oa, source="orcid", extra_aliases=orcid_aliases
            )
        else:
            if not display and not orcid_aliases:
                raise AuthorResolveError(
                    f"orcid {value}: no data from ORCID or OpenAlex"
                )
            aliases = sorted({a for a in ([display] + orcid_aliases) if a})
            author = WhitelistedAuthor(
                id=f"https://orcid.org/{value}",
                display_name=display or value,
                name_aliases=aliases,
                orcid=value,
                source="orcid",
            )
    elif kind == "scholar":
        name, affiliation = fetch_scholar_profile(value)
        oa = fetch_openalex_author_by_name(name, mailto=mailto)
        if oa:
            author = _author_from_openalex(oa, source="scholar", extra_aliases=[name])
            author.affiliation = author.affiliation or affiliation
        else:
            author = WhitelistedAuthor(
                id=f"name:{normalize_author_name(name)}",
                display_name=name,
                name_aliases=[name],
                affiliation=affiliation,
                source="scholar",
            )
    else:
        raise AuthorResolveError(
            "unrecognized input; send an ORCID id/URL or a Google Scholar profile URL"
        )
    author.added_by = added_by
    return author
