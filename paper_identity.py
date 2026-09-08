"""Stable paper identity keys (DOI, arXiv id, normalized URL) for seen-set and dedup."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from openalex_enrich import extract_arxiv_id, extract_doi_from_link
from rss_merge import normalize_link

if TYPE_CHECKING:
    from openalex_enrich import PaperEnrichment

_ACS_TAIL = re.compile(r"/(\d+)/[A-Za-z].*$")


def _canonical_doi(raw: str) -> str:
    doi = raw.lower().strip()
    doi = doi.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
    doi = doi.removeprefix("https://dx.doi.org/").removeprefix("http://dx.doi.org/")
    doi = _ACS_TAIL.sub("", doi)
    return doi.rstrip(".,;)/")


def identity_keys(link: str, enrichment: PaperEnrichment | None = None) -> set[str]:
    """Return ``url:`` / ``doi:`` / ``arxiv:`` keys that identify one paper."""
    keys: set[str] = set()
    url = str(link or "").strip()
    if url:
        keys.add("url:" + normalize_link(url))
        doi = extract_doi_from_link(url)
        if doi:
            keys.add("doi:" + _canonical_doi(doi))
        aid = extract_arxiv_id(url)
        if aid:
            keys.add("arxiv:" + aid.lower())
    if enrichment is not None:
        if enrichment.arxiv_url:
            keys.add("url:" + normalize_link(str(enrichment.arxiv_url).strip()))
            aid = extract_arxiv_id(str(enrichment.arxiv_url))
            if aid:
                keys.add("arxiv:" + aid.lower())
        if enrichment.doi:
            keys.add("doi:" + _canonical_doi(str(enrichment.doi)))
    return keys


def cluster_links_by_identity(link_key_sets: dict[str, set[str]]) -> list[set[str]]:
    """Group original link strings whose identity key sets intersect."""
    if not link_key_sets:
        return []
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        pa, pb = find(a), find(b)
        if pa != pb:
            parent[pb] = pa

    for link, keys in link_key_sets.items():
        node = f"link:{link}"
        items = [node, *keys]
        for item in items[1:]:
            union(items[0], item)

    groups: dict[str, set[str]] = {}
    for link in link_key_sets:
        groups.setdefault(find(f"link:{link}"), set()).add(link)
    return list(groups.values())
