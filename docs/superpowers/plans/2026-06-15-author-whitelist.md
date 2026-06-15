# Author Whitelist + Bot-Managed Authors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Always include articles authored by whitelisted researchers (bypassing LLM score thresholds), and let the team manage that whitelist by posting ORCID/Google-Scholar links in a Zulip topic.

**Architecture:** Three new modules — `author_whitelist.py` (JSON store + name/ID matcher), `author_resolve.py` (ORCID/Scholar → canonical author record via ORCID + OpenAlex APIs), `author_whitelist_bot.py` (per-run Zulip command poll with an idempotency cursor) — plus two small hooks in `main.py` (run the bot once per run; merge whitelist-matched articles into each group's `passing` set).

**Tech Stack:** Python 3, pydantic, requests, feedparser, `zulip` SDK, `unittest` + `unittest.mock` (existing test style), TOML config.

**Design reference:** `docs/superpowers/specs/2026-06-15-author-whitelist-design.md`

**Conventions to follow:**
- Tests use `unittest.TestCase` + `unittest.mock.patch`, one file per module under `tests/`.
- Reuse `rss_merge.normalize_link` for links and `openalex_enrich._norm_person_name` for names.
- Reuse `openalex_enrich.OPENALEX_BASE`, `api_usage.record_openalex_http`, and `zulip_context` helpers (`_client_for_realm`, `fetch_messages_narrow`).
- Atomic file writes (temp file + `os.replace`).
- Run tests with: `python -m pytest tests/ -q` (or `python -m unittest`).

---

## File structure

| File | Responsibility |
|------|----------------|
| `author_whitelist.py` (new) | `WhitelistedAuthor` model, `AuthorWhitelist` store (load/save/add/remove/cursor), name + OpenAlex-id matching, name-splitting/normalization helpers. |
| `author_resolve.py` (new) | Parse ORCID/Scholar input; fetch ORCID + OpenAlex; build a canonical `WhitelistedAuthor`. `AuthorResolveError` on failure. |
| `author_whitelist_bot.py` (new) | Parse `add`/`remove`/`list` commands, poll the Zulip command topic once per run, reply in-topic, advance the idempotency cursor. |
| `main.py` (modify) | `process_group`: merge whitelist-matched articles into `passing`. `main()`: run the bot poll once per run; load whitelist config. |
| `config.d/config.toml.example` (modify) | Document the `[author_whitelist]` section. |
| `README.md` (modify) | Short "Author whitelist" usage section. |
| `tests/test_author_whitelist.py` etc. (new) | One test file per new module + a pipeline integration test. |

---

### Task 1: Whitelist store + matcher (`author_whitelist.py`)

**Files:**
- Create: `author_whitelist.py`
- Test: `tests/test_author_whitelist.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_author_whitelist.py
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from adapter import ArticleInfo
from author_whitelist import (
    AuthorWhitelist,
    WhitelistedAuthor,
    normalize_author_name,
    split_author_names,
)


def _article(authors: str) -> ArticleInfo:
    return ArticleInfo(
        title="t",
        link="https://www.nature.com/articles/s41586-026-10636-y",
        abstract="a",
        updated=datetime(2026, 6, 15, tzinfo=timezone.utc),
        authors=authors,
    )


def _author(**kw) -> WhitelistedAuthor:
    base = dict(
        id="https://orcid.org/0000-0002-1825-0097",
        display_name="Josiah Carberry",
        name_aliases=["Josiah Carberry", "J. Carberry"],
        orcid="0000-0002-1825-0097",
        openalex_id="A5023888391",
    )
    base.update(kw)
    return WhitelistedAuthor(**base)


class TestHelpers(unittest.TestCase):
    def test_normalize_author_name(self):
        self.assertEqual(normalize_author_name("  Josiah   Carberry "), "josiah carberry")

    def test_split_author_names(self):
        self.assertEqual(
            split_author_names("Alice Smith, Bob Jones and C. Doe"),
            ["Alice Smith", "Bob Jones", "C. Doe"],
        )
        self.assertEqual(split_author_names(""), [])


class TestStore(unittest.TestCase):
    def test_save_load_round_trip(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "wl.json"
            wl = AuthorWhitelist()
            wl.add(_author())
            wl.set_cursor("tuesday:science:author whitelist", 42)
            wl.save(p)
            loaded = AuthorWhitelist.load(p)
            self.assertEqual(len(loaded.authors), 1)
            self.assertEqual(loaded.get_cursor("tuesday:science:author whitelist"), 42)

    def test_load_missing_file_is_empty(self):
        with TemporaryDirectory() as d:
            self.assertEqual(AuthorWhitelist.load(Path(d) / "nope.json").authors, [])

    def test_load_malformed_is_empty(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "bad.json"
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(AuthorWhitelist.load(p).authors, [])

    def test_add_is_idempotent_and_merges_aliases(self):
        wl = AuthorWhitelist()
        self.assertTrue(wl.add(_author()))
        self.assertFalse(wl.add(_author(name_aliases=["Josiah S. Carberry"])))
        self.assertEqual(len(wl.authors), 1)
        self.assertIn("Josiah S. Carberry", wl.authors[0].name_aliases)

    def test_remove_by_orcid_and_name(self):
        wl = AuthorWhitelist()
        wl.add(_author())
        self.assertIsNotNone(wl.remove("0000-0002-1825-0097"))
        self.assertEqual(wl.authors, [])
        wl.add(_author())
        self.assertIsNotNone(wl.remove("josiah carberry"))
        self.assertEqual(wl.authors, [])

    def test_cursor_only_advances(self):
        wl = AuthorWhitelist()
        wl.set_cursor("k", 10)
        wl.set_cursor("k", 5)
        self.assertEqual(wl.get_cursor("k"), 10)


class TestMatching(unittest.TestCase):
    def test_matches_alias_case_and_whitespace(self):
        wl = AuthorWhitelist()
        wl.add(_author())
        self.assertIsNotNone(wl.matches(_article("Alice Smith, josiah   carberry")))

    def test_no_match(self):
        wl = AuthorWhitelist()
        wl.add(_author())
        self.assertIsNone(wl.matches(_article("Alice Smith, Bob Jones")))

    def test_empty_whitelist_never_matches(self):
        self.assertIsNone(AuthorWhitelist().matches(_article("Josiah Carberry")))

    def test_matches_openalex_author_ids(self):
        wl = AuthorWhitelist()
        wl.add(_author())
        self.assertIsNotNone(wl.matches_openalex_author_ids(["A999", "A5023888391"]))
        self.assertIsNone(wl.matches_openalex_author_ids(["A999"]))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_author_whitelist.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'author_whitelist'`.

- [ ] **Step 3: Write `author_whitelist.py`**

```python
"""Author whitelist store + matcher.

Articles authored by a whitelisted researcher are always included in feed output,
bypassing the LLM relevance/impact thresholds. Identity is resolved once when an
author is added (see author_resolve.py); scan-time matching is by normalized name
(no network), with an optional OpenAlex-author-id path.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, Field

from adapter import ArticleInfo

logger = logging.getLogger(__name__)

WHITELIST_VERSION = 1


def normalize_author_name(name: str) -> str:
    """Lowercase + collapse whitespace; mirrors openalex_enrich._norm_person_name."""
    t = str(name).strip().lower()
    return re.sub(r"\s+", " ", t)


def split_author_names(authors: str) -> list[str]:
    """Split an RSS author string into individual names."""
    if not authors:
        return []
    parts = re.split(r"\s*(?:,|;|&|\band\b)\s*", authors)
    return [p.strip() for p in parts if p.strip()]


class WhitelistedAuthor(BaseModel):
    id: str
    display_name: str
    name_aliases: list[str] = Field(default_factory=list)
    orcid: str | None = None
    openalex_id: str | None = None
    affiliation: str | None = None
    works_count: int | None = None
    source: str = "manual"
    added_by: str | None = None
    added_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def normalized_aliases(self) -> set[str]:
        out = {normalize_author_name(a) for a in self.name_aliases if a.strip()}
        out.add(normalize_author_name(self.display_name))
        return {a for a in out if a}


class AuthorWhitelist(BaseModel):
    version: int = WHITELIST_VERSION
    authors: list[WhitelistedAuthor] = Field(default_factory=list)
    cursor: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "AuthorWhitelist":
        if not Path(path).is_file():
            return cls()
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls.model_validate(data)
        except Exception:
            logger.exception(
                "Malformed author whitelist at %s; treating as empty", path
            )
            return cls()

    def save(self, path: Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.model_dump(), fh, indent=2, ensure_ascii=False)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def add(self, author: WhitelistedAuthor) -> bool:
        for existing in self.authors:
            if existing.id == author.id:
                existing.name_aliases = sorted(
                    set(existing.name_aliases) | set(author.name_aliases)
                )
                existing.affiliation = author.affiliation or existing.affiliation
                existing.openalex_id = author.openalex_id or existing.openalex_id
                existing.orcid = author.orcid or existing.orcid
                existing.works_count = author.works_count or existing.works_count
                return False
        self.authors.append(author)
        return True

    def remove(self, token: str) -> WhitelistedAuthor | None:
        t = token.strip()
        tn = normalize_author_name(t)
        for i, a in enumerate(self.authors):
            if t and t in (a.id, a.orcid, a.openalex_id):
                return self.authors.pop(i)
            if normalize_author_name(a.display_name) == tn:
                return self.authors.pop(i)
        return None

    def _alias_to_author(self) -> dict[str, WhitelistedAuthor]:
        index: dict[str, WhitelistedAuthor] = {}
        for a in self.authors:
            for alias in a.normalized_aliases():
                index.setdefault(alias, a)
        return index

    def matches(self, article: ArticleInfo) -> WhitelistedAuthor | None:
        if not self.authors:
            return None
        index = self._alias_to_author()
        for name in split_author_names(article.authors or ""):
            hit = index.get(normalize_author_name(name))
            if hit is not None:
                return hit
        return None

    def matches_openalex_author_ids(
        self, ids: Iterable[str]
    ) -> WhitelistedAuthor | None:
        wanted = {i for i in ids if i}
        if not wanted:
            return None
        for a in self.authors:
            if a.openalex_id and a.openalex_id in wanted:
                return a
        return None

    def get_cursor(self, key: str) -> int:
        return int(self.cursor.get(key, 0))

    def set_cursor(self, key: str, message_id: int) -> None:
        if int(message_id) > self.get_cursor(key):
            self.cursor[key] = int(message_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_author_whitelist.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add author_whitelist.py tests/test_author_whitelist.py
git commit -m "feat: add author whitelist store and name matcher"
```

---

### Task 2: Author resolver (`author_resolve.py`)

Turns an ORCID id/URL or a Google Scholar profile URL into a canonical
`WhitelistedAuthor`, capturing name aliases (for robust scan-time matching),
ORCID, OpenAlex id, affiliation, and works count.

**Files:**
- Create: `author_resolve.py`
- Test: `tests/test_author_resolve.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_author_resolve.py
import unittest
from unittest.mock import MagicMock, patch

import author_resolve as ar
from author_resolve import AuthorResolveError, parse_author_input


def _resp(json_data=None, text="", status=200):
    r = MagicMock()
    r.json.return_value = json_data or {}
    r.text = text
    r.status_code = status
    r.raise_for_status = MagicMock()
    return r


_OA_AUTHOR = {
    "id": "https://openalex.org/A5023888391",
    "display_name": "Josiah Carberry",
    "display_name_alternatives": ["J. Carberry", "Josiah S. Carberry"],
    "orcid": "https://orcid.org/0000-0002-1825-0097",
    "works_count": 142,
    "last_known_institutions": [{"display_name": "Brown University"}],
}


class TestParseInput(unittest.TestCase):
    def test_parse_orcid_bare_and_url(self):
        self.assertEqual(
            parse_author_input("0000-0002-1825-0097"),
            ("orcid", "0000-0002-1825-0097"),
        )
        self.assertEqual(
            parse_author_input("https://orcid.org/0000-0002-1825-0097"),
            ("orcid", "0000-0002-1825-0097"),
        )

    def test_parse_scholar(self):
        kind, val = parse_author_input(
            "https://scholar.google.com/citations?user=ABCdef123&hl=en"
        )
        self.assertEqual(kind, "scholar")
        self.assertEqual(val, "ABCdef123")

    def test_parse_unknown(self):
        self.assertEqual(parse_author_input("just a name")[0], "unknown")


class TestResolve(unittest.TestCase):
    @patch("author_resolve.requests.get")
    def test_resolve_orcid(self, mock_get):
        # 1st call: ORCID person; 2nd: OpenAlex author-by-orcid
        mock_get.side_effect = [
            _resp(
                {
                    "name": {
                        "given-names": {"value": "Josiah"},
                        "family-name": {"value": "Carberry"},
                        "other-names": {"other-name": [{"content": "J. Carberry"}]},
                    }
                }
            ),
            _resp(_OA_AUTHOR),
        ]
        a = ar.resolve("https://orcid.org/0000-0002-1825-0097", mailto="me@x.com")
        self.assertEqual(a.id, "https://orcid.org/0000-0002-1825-0097")
        self.assertEqual(a.openalex_id, "A5023888391")
        self.assertEqual(a.affiliation, "Brown University")
        self.assertEqual(a.works_count, 142)
        self.assertIn("J. Carberry", a.name_aliases)
        self.assertEqual(a.source, "orcid")

    @patch("author_resolve.requests.get")
    def test_resolve_orcid_without_openalex(self, mock_get):
        mock_get.side_effect = [
            _resp(
                {
                    "name": {
                        "given-names": {"value": "Jane"},
                        "family-name": {"value": "Doe"},
                    }
                }
            ),
            _resp(status=404),  # OpenAlex miss
        ]
        a = ar.resolve("0000-0002-0000-0000", mailto="me@x.com")
        self.assertEqual(a.display_name, "Jane Doe")
        self.assertIsNone(a.openalex_id)
        self.assertEqual(a.source, "orcid")

    @patch("author_resolve.requests.get")
    def test_resolve_scholar(self, mock_get):
        scholar_html = (
            '<div id="gsc_prf_in">Josiah Carberry</div>'
            '<div class="gsc_prf_il">Brown University</div>'
        )
        mock_get.side_effect = [
            _resp(text=scholar_html),
            _resp({"results": [_OA_AUTHOR]}),  # OpenAlex search
        ]
        a = ar.resolve(
            "https://scholar.google.com/citations?user=ABC", mailto="me@x.com"
        )
        self.assertEqual(a.openalex_id, "A5023888391")
        self.assertEqual(a.source, "scholar")

    def test_resolve_unknown_raises(self):
        with self.assertRaises(AuthorResolveError):
            ar.resolve("not an id", mailto="me@x.com")

    @patch("author_resolve.requests.get")
    def test_resolve_scholar_scrape_failure_raises(self, mock_get):
        mock_get.side_effect = [_resp(text="<html>captcha</html>")]
        with self.assertRaises(AuthorResolveError):
            ar.resolve("https://scholar.google.com/citations?user=ABC", mailto="me@x.com")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_author_resolve.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'author_resolve'`.

- [ ] **Step 3: Write `author_resolve.py`**

```python
"""Resolve an ORCID id/URL or Google Scholar profile URL to a WhitelistedAuthor.

Used only when ADDING an author (low volume), so it may make a few HTTP calls and
fall back gracefully. Scan-time matching never calls this module.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlparse

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
    """Return (display_name, aliases) from the ORCID public API; ([],) on failure."""
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
    name = (data.get("name") or {})
    given = ((name.get("given-names") or {}) or {}).get("value", "")
    family = ((name.get("family-name") or {}) or {}).get("value", "")
    display = " ".join(p for p in (given, family) if p).strip()
    aliases: list[str] = []
    others = ((name.get("other-names") or {}) or {}).get("other-name") or []
    for o in others:
        c = (o or {}).get("content")
        if c:
            aliases.append(c)
    return (display, aliases)


def _author_from_openalex(oa: dict, *, source: str, extra_aliases=()) -> WhitelistedAuthor:
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
    from urllib.parse import quote

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


def resolve(input_str: str, *, mailto: str | None, added_by: str | None = None) -> WhitelistedAuthor:
    kind, value = parse_author_input(input_str)
    if kind == "orcid":
        display, orcid_aliases = fetch_orcid_person(value)
        oa = fetch_openalex_author_by_orcid(value, mailto=mailto)
        if oa:
            author = _author_from_openalex(oa, source="orcid", extra_aliases=orcid_aliases)
        else:
            if not display and not orcid_aliases:
                raise AuthorResolveError(f"orcid {value}: no data from ORCID or OpenAlex")
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_author_resolve.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add author_resolve.py tests/test_author_resolve.py
git commit -m "feat: resolve ORCID/Scholar inputs to whitelist authors"
```

---

### Task 3: Bot command loop (`author_whitelist_bot.py`)

Polls the configured Zulip topic once per run, parses `add`/`remove`/`list`,
mutates the whitelist, replies in-topic, and advances the per-topic cursor so each
command is handled exactly once.

**Files:**
- Create: `author_whitelist_bot.py`
- Test: `tests/test_author_whitelist_bot.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_author_whitelist_bot.py
import time
import unittest
from unittest.mock import MagicMock, patch

from author_whitelist import AuthorWhitelist, WhitelistedAuthor
from author_whitelist_bot import parse_command, run_author_whitelist_bot

REALMS = {"tuesday": {"email": "bot@x.com", "api_key": "k", "site": "https://x"}}
SOURCE = {
    "realm": "tuesday",
    "stream": "science",
    "topic": "author whitelist",
    "lookback_hours": 168,
    "max_messages": 200,
}


def _msg(mid, content, sender="alice@x.com"):
    return {
        "id": mid,
        "timestamp": int(time.time()),
        "content": content,
        "sender_email": sender,
    }


def _fake_client(messages):
    c = MagicMock()
    c.get_messages.return_value = {"result": "success", "messages": messages}
    c.send_message.return_value = {"result": "success"}
    return c


def _author():
    return WhitelistedAuthor(
        id="https://orcid.org/0000-0002-1825-0097",
        display_name="Josiah Carberry",
        name_aliases=["Josiah Carberry"],
        orcid="0000-0002-1825-0097",
        openalex_id="A5023888391",
        affiliation="Brown University",
        works_count=142,
    )


class TestParseCommand(unittest.TestCase):
    def test_add(self):
        self.assertEqual(
            parse_command("add https://orcid.org/0000-0002-1825-0097"),
            ("add", "https://orcid.org/0000-0002-1825-0097"),
        )

    def test_add_with_mention(self):
        self.assertEqual(parse_command("@bot add 0000"), ("add", "0000"))

    def test_remove_and_list(self):
        self.assertEqual(parse_command("remove josiah carberry"), ("remove", "josiah carberry"))
        self.assertEqual(parse_command("list"), ("list", ""))

    def test_non_command(self):
        self.assertIsNone(parse_command("hello team, nice paper"))


class TestRunBot(unittest.TestCase):
    @patch("author_whitelist_bot._client_for_realm")
    @patch("author_whitelist_bot.resolve")
    def test_add_flow(self, mock_resolve, mock_client_for):
        mock_resolve.return_value = _author()
        client = _fake_client([_msg(101, "add https://orcid.org/0000-0002-1825-0097")])
        mock_client_for.return_value = client
        wl = AuthorWhitelist()
        changed = run_author_whitelist_bot(
            wl, command_source=SOURCE, realms=REALMS, mailto="me@x.com", dryrun=False
        )
        self.assertTrue(changed)
        self.assertEqual(len(wl.authors), 1)
        client.send_message.assert_called_once()
        sent = client.send_message.call_args[0][0]
        self.assertEqual(sent["topic"], "author whitelist")
        self.assertEqual(wl.get_cursor("tuesday:science:author whitelist"), 101)

    @patch("author_whitelist_bot._client_for_realm")
    @patch("author_whitelist_bot.resolve")
    def test_idempotent_second_run(self, mock_resolve, mock_client_for):
        mock_resolve.return_value = _author()
        client = _fake_client([_msg(101, "add 0000-0002-1825-0097")])
        mock_client_for.return_value = client
        wl = AuthorWhitelist()
        wl.set_cursor("tuesday:science:author whitelist", 101)
        changed = run_author_whitelist_bot(
            wl, command_source=SOURCE, realms=REALMS, mailto="me@x.com", dryrun=False
        )
        self.assertFalse(changed)
        self.assertEqual(wl.authors, [])
        client.send_message.assert_not_called()

    @patch("author_whitelist_bot._client_for_realm")
    @patch("author_whitelist_bot.resolve")
    def test_skips_bot_own_messages(self, mock_resolve, mock_client_for):
        client = _fake_client([_msg(102, "add 0000", sender="bot@x.com")])
        mock_client_for.return_value = client
        wl = AuthorWhitelist()
        run_author_whitelist_bot(
            wl, command_source=SOURCE, realms=REALMS, mailto="me@x.com", dryrun=False
        )
        mock_resolve.assert_not_called()
        self.assertEqual(wl.authors, [])

    @patch("author_whitelist_bot._client_for_realm")
    @patch("author_whitelist_bot.resolve")
    def test_dryrun_does_not_send(self, mock_resolve, mock_client_for):
        mock_resolve.return_value = _author()
        client = _fake_client([_msg(101, "add 0000")])
        mock_client_for.return_value = client
        wl = AuthorWhitelist()
        run_author_whitelist_bot(
            wl, command_source=SOURCE, realms=REALMS, mailto="me@x.com", dryrun=True
        )
        client.send_message.assert_not_called()
        self.assertEqual(len(wl.authors), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_author_whitelist_bot.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'author_whitelist_bot'`.

- [ ] **Step 3: Write `author_whitelist_bot.py`**

```python
"""Zulip command loop for managing the author whitelist.

Runs once per cron cycle: reads new messages in a dedicated topic, applies
add/remove/list commands, replies, and advances an idempotency cursor.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from author_resolve import AuthorResolveError, resolve
from author_whitelist import AuthorWhitelist
from zulip_context import _client_for_realm, fetch_messages_narrow

logger = logging.getLogger(__name__)

_DEFAULT_LOOKBACK_HOURS = 168
_DEFAULT_MAX_MESSAGES = 200


def parse_command(content: str) -> tuple[str, str] | None:
    """Return (action, arg) for add/remove/list, else None."""
    text = (content or "").strip()
    text = re.sub(r"^@[*]{0,2}[\w .-]+[*]{0,2}\s*", "", text).strip()  # drop leading mention
    if not text:
        return None
    low = text.lower()
    if low == "list" or low.startswith("list "):
        return ("list", "")
    for action in ("add", "remove"):
        if low == action or low.startswith(action + " "):
            return (action, text[len(action):].strip())
    return None


def _message_text(msg: dict[str, Any]) -> str:
    raw = msg.get("content") or ""
    try:
        return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    except Exception:
        return raw


def format_added_reply(author, added: bool) -> str:
    verb = "Added" if added else "Updated"
    aff = f" ({author.affiliation})" if author.affiliation else ""
    bits = []
    if author.orcid:
        bits.append(f"ORCID {author.orcid}")
    if author.openalex_id:
        bits.append(f"OpenAlex {author.openalex_id}")
    if author.works_count is not None:
        bits.append(f"{author.works_count} works")
    tail = ("\n   " + " · ".join(bits)) if bits else ""
    return (
        f"Added author **{author.display_name}**{aff}{tail}\n"
        f"   Their papers will now always be included regardless of score."
        if verb == "Added"
        else f"Updated author **{author.display_name}**{aff}{tail}"
    )


def format_removed_reply(author) -> str:
    return f"Removed **{author.display_name}** from the whitelist."


def format_list_reply(wl: AuthorWhitelist) -> str:
    if not wl.authors:
        return "Author whitelist is empty."
    lines = ["Author whitelist:"]
    for a in wl.authors:
        aff = f" — {a.affiliation}" if a.affiliation else ""
        ident = a.orcid or a.openalex_id or a.id
        lines.append(f"- {a.display_name}{aff} ({ident})")
    return "\n".join(lines)


def format_error_reply(msg: str) -> str:
    return (
        f"Could not process that: {msg}\n"
        "Usage: `add <ORCID id/URL or Google Scholar profile URL>`, "
        "`remove <ORCID/OpenAlex id/name>`, or `list`."
    )


def _send(client, stream: str, topic: str, content: str, dryrun: bool) -> None:
    if dryrun:
        logger.info("[author-whitelist dryrun] would reply: %s", content)
        return
    try:
        client.send_message(
            {"type": "stream", "to": stream, "topic": topic, "content": content}
        )
    except Exception:
        logger.exception("Failed to send author-whitelist reply")


def run_author_whitelist_bot(
    whitelist: AuthorWhitelist,
    *,
    command_source: dict[str, Any],
    realms: dict[str, dict[str, str]],
    mailto: str | None,
    dryrun: bool,
) -> bool:
    """Process new commands in the configured topic. Returns True if changed."""
    realm = command_source.get("realm")
    stream = command_source.get("stream")
    topic = command_source.get("topic") or "author whitelist"
    if not realm or not stream:
        logger.warning("[author-whitelist] command_source missing realm/stream; skipping")
        return False
    try:
        client = _client_for_realm(realms, realm)
    except Exception:
        logger.exception("[author-whitelist] could not create Zulip client for %s", realm)
        return False

    bot_email = (realms.get(realm) or {}).get("email", "").lower()
    key = f"{realm}:{stream}:{topic}"
    cursor = whitelist.get_cursor(key)

    msgs = fetch_messages_narrow(
        client,
        stream,
        topic,
        int(command_source.get("lookback_hours", _DEFAULT_LOOKBACK_HOURS)),
        int(command_source.get("max_messages", _DEFAULT_MAX_MESSAGES)),
    )
    msgs = sorted(msgs, key=lambda m: m.get("id", 0))

    changed = False
    for msg in msgs:
        mid = int(msg.get("id", 0))
        if mid <= cursor:
            continue
        if (msg.get("sender_email") or "").lower() == bot_email:
            whitelist.set_cursor(key, mid)
            continue
        cmd = parse_command(_message_text(msg))
        if cmd is None:
            whitelist.set_cursor(key, mid)
            continue
        action, arg = cmd
        try:
            if action == "list":
                _send(client, stream, topic, format_list_reply(whitelist), dryrun)
            elif action == "remove":
                removed = whitelist.remove(arg)
                if removed is not None:
                    changed = True
                    _send(client, stream, topic, format_removed_reply(removed), dryrun)
                else:
                    _send(client, stream, topic, format_error_reply(f"no whitelist entry matched '{arg}'"), dryrun)
            elif action == "add":
                author = resolve(arg, mailto=mailto, added_by=msg.get("sender_email"))
                added = whitelist.add(author)
                changed = True
                _send(client, stream, topic, format_added_reply(author, added), dryrun)
        except AuthorResolveError as e:
            _send(client, stream, topic, format_error_reply(str(e)), dryrun)
        except Exception as e:
            logger.exception("[author-whitelist] command failed: %s", arg)
            _send(client, stream, topic, format_error_reply(f"unexpected error ({e})"), dryrun)
        whitelist.set_cursor(key, mid)
    return changed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_author_whitelist_bot.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add author_whitelist_bot.py tests/test_author_whitelist_bot.py
git commit -m "feat: add Zulip bot loop to manage author whitelist"
```

---

### Task 4: Pipeline integration (`main.py` + force-include helper)

Adds a pure, testable helper to `author_whitelist.py`, then wires the whitelist
into `process_group` (force-include) and `main()` (per-run bot poll).

**Files:**
- Modify: `author_whitelist.py` (add `force_included_whitelist_items`)
- Modify: `main.py` (imports, `process_group` param + merge, `main()` bot poll + call)
- Test: `tests/test_whitelist_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_whitelist_pipeline.py
import unittest
from datetime import datetime, timezone

from adapter import ArticleInfo
from fastgpt_reply import Reply
from author_whitelist import (
    AuthorWhitelist,
    WhitelistedAuthor,
    force_included_whitelist_items,
)


def _art(link, authors):
    return ArticleInfo(
        title="t",
        link=link,
        abstract="a",
        updated=datetime(2026, 6, 15, tzinfo=timezone.utc),
        authors=authors,
    )


class TestForceInclude(unittest.TestCase):
    def setUp(self):
        self.wl = AuthorWhitelist()
        self.wl.add(
            WhitelistedAuthor(
                id="https://orcid.org/0000-0002-1825-0097",
                display_name="Josiah Carberry",
                name_aliases=["Josiah Carberry"],
            )
        )

    def test_low_scored_whitelisted_article_is_force_included(self):
        arts = [_art("https://www.nature.com/articles/x", "Josiah Carberry, Bob")]
        replies = [Reply(relevance=0, impact=0, reason="not shortlisted")]
        add = force_included_whitelist_items(arts, replies, [], self.wl)
        self.assertEqual(len(add), 1)
        self.assertEqual(str(add[0][0].link), str(arts[0].link))
        self.assertIn("whitelisted author", add[0][1].reason)

    def test_non_whitelisted_not_included(self):
        arts = [_art("https://www.nature.com/articles/y", "Alice, Bob")]
        replies = [Reply(relevance=0, impact=0)]
        self.assertEqual(force_included_whitelist_items(arts, replies, [], self.wl), [])

    def test_already_passing_not_duplicated(self):
        a = _art("https://www.nature.com/articles/x", "Josiah Carberry")
        replies = [Reply(relevance=8, impact=7)]
        self.assertEqual(
            force_included_whitelist_items([a], replies, [(a, replies[0])], self.wl), []
        )

    def test_none_whitelist(self):
        a = _art("https://www.nature.com/x", "Josiah Carberry")
        self.assertEqual(
            force_included_whitelist_items([a], [Reply(relevance=0, impact=0)], [], None),
            [],
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_whitelist_pipeline.py -q`
Expected: FAIL with `ImportError: cannot import name 'force_included_whitelist_items'`.

- [ ] **Step 3: Add the helper to `author_whitelist.py`**

Append at the end of `author_whitelist.py` (lazy imports avoid pulling `rss_merge`/`fastgpt_reply` at module import time):

```python
def force_included_whitelist_items(recent_articles, replies, passing, whitelist):
    """Return (article, Reply, WhitelistedAuthor) tuples to ADD to ``passing``.

    A whitelist hit reuses the article's real score (so cross-group dedup can place
    it well) and tags the reason. Links already in ``passing`` are skipped.
    """
    from fastgpt_reply import Reply
    from rss_merge import normalize_link

    out = []
    if whitelist is None or not whitelist.authors:
        return out
    passing_links = {normalize_link(str(a.link)) for a, _ in passing}
    for article, reply in zip(recent_articles, replies):
        nl = normalize_link(str(article.link))
        if nl in passing_links:
            continue
        hit = whitelist.matches(article)
        if hit is None:
            continue
        out.append(
            (
                article,
                Reply(
                    relevance=reply.relevance,
                    impact=reply.impact,
                    reason=f"whitelisted author: {hit.display_name}",
                ),
                hit,
            )
        )
        passing_links.add(nl)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_whitelist_pipeline.py -q`
Expected: PASS.

- [ ] **Step 5: Wire into `main.py` — imports**

After the existing line `from zulip_journal_weekly_summary import maybe_post_weekly_journal_config_summary` (line 76), add:

```python
from author_whitelist import AuthorWhitelist, force_included_whitelist_items
from author_whitelist_bot import run_author_whitelist_bot
```

- [ ] **Step 6: Wire into `main.py` — `process_group` signature**

Change the keyword-only params of `process_group` (lines 414-418) to add `author_whitelist`:

```python
    *,
    kagi_prefilter_cap: int = 20,
    kagi_batch_size: int = 5,
    openrouter: OpenRouterClient | None = None,
    route_to_openrouter: list[str] | None = None,
    author_whitelist: "AuthorWhitelist | None" = None,
) -> GroupRunResult:
```

- [ ] **Step 7: Wire into `main.py` — force-include after threshold**

Immediately after the `passing` list comprehension (lines 597-601), insert:

```python
    for article, forced_reply, hit in force_included_whitelist_items(
        recent_articles, replies, passing, author_whitelist
    ):
        passing.append((article, forced_reply))
        logger.info(
            "[%s] author whitelist: force-including %r (matched %s)",
            group_name,
            article.title,
            hit.display_name,
        )
```

- [ ] **Step 8: Wire into `main.py` — `main()` bot poll**

After the `zulip_realms = load_zulip_realms(...)` assignment (ends line 723) and
before `groups = expand_groups(cfg)` (line 725), insert:

```python
        author_whitelist = None
        aw_cfg = cfg.get("author_whitelist") or {}
        if aw_cfg and aw_cfg.get("enabled", True):
            wl_file = aw_cfg.get("file", "author_whitelist.json")
            wl_path = Path(wl_file)
            if not wl_path.is_absolute():
                wl_path = (config_path.parent / wl_path).resolve()
            author_whitelist = AuthorWhitelist.load(wl_path)
            command_source = aw_cfg.get("command_source")
            if command_source and zulip_realms:
                aw_mailto = openalex_cfg.get("mailto") or os.environ.get("OPENALEX_MAILTO")
                try:
                    changed = run_author_whitelist_bot(
                        author_whitelist,
                        command_source=command_source,
                        realms=zulip_realms,
                        mailto=aw_mailto,
                        dryrun=dryrun,
                    )
                    if changed and not dryrun:
                        author_whitelist.save(wl_path)
                except Exception:
                    logger.exception("[author-whitelist] bot poll failed")
```

- [ ] **Step 9: Wire into `main.py` — pass whitelist to `process_group`**

In the `process_group(...)` call inside the group loop, after the line
`route_to_openrouter=route_to_openrouter,` (line 748), add:

```python
                    author_whitelist=author_whitelist,
```

- [ ] **Step 10: Run the full suite + a dry run**

Run: `python -m pytest tests/ -q`
Expected: PASS (all existing + new tests).

Run: `python main.py --config-path config.d/config.toml --dryrun`
Expected: completes without error; with `[author_whitelist]` absent, behavior is unchanged. (This makes live network calls; if offline, rely on the pytest suite.)

- [ ] **Step 11: Commit**

```bash
git add author_whitelist.py main.py tests/test_whitelist_pipeline.py
git commit -m "feat: force-include whitelisted authors and run bot per cron cycle"
```

---

### Task 5: Config template + README docs

**Files:**
- Modify: `config.d/config.toml.example`
- Modify: `README.md`

- [ ] **Step 1: Document the section in `config.toml.example`**

After the `[zulip]` block (immediately before the `# Topical group 1:` comment),
insert:

```toml
# Author whitelist: papers by these authors are ALWAYS included, bypassing the
# relevance/impact thresholds. Manage it from Zulip by posting in `command_source`:
#   add https://orcid.org/0000-0002-1825-0097
#   add https://scholar.google.com/citations?user=XXXX
#   remove 0000-0002-1825-0097     (or an OpenAlex id, or a display name)
#   list
# The whitelist file is JSON, stored next to this config (gitignored as local state).
# [author_whitelist]
# enabled = true
# file = "author_whitelist.json"
# command_source = { realm = "tuesday", stream = "science", topic = "author whitelist", lookback_hours = 168, max_messages = 200 }
```

- [ ] **Step 2: Add a README section**

Add a `## Author whitelist` section to `README.md` (e.g. after the Zulip section):

````markdown
## Author whitelist

Papers by specific researchers can be **always included**, bypassing the LLM
relevance/impact thresholds. Matching is by author name (captured with aliases
from ORCID/OpenAlex when the author is added), so it fires immediately — even on
same-day articles OpenAlex hasn't indexed yet.

Enable it with an `[author_whitelist]` section (see `config.d/config.toml.example`).
The whitelist is a JSON file (`config.d/author_whitelist.json`, gitignored).

Manage it by posting in the configured Zulip topic (default `author whitelist`):

```
add https://orcid.org/0000-0002-1825-0097
add https://scholar.google.com/citations?user=XXXX
remove 0000-0002-1825-0097
list
```

The bot resolves the identity (ORCID + OpenAlex), adds the author, and replies
with what it matched so you can verify; `remove` undoes a wrong match. Commands
are processed once per cron run.
````

- [ ] **Step 3: Commit**

```bash
git add config.d/config.toml.example README.md
git commit -m "docs: document author whitelist config and bot commands"
```

- [ ] **Step 4 (manual, not committed): enable in the live config**

`config.d/config.toml` is gitignored. To turn the feature on in production, add an
`[author_whitelist]` section there (top level, sibling to `[kagi]`/`[zulip]`) with a
`command_source` pointing at a real realm/stream the team can post to (e.g.
`realm = "tuesday", stream = "science"`). Do **not** commit this file.

---

## Self-Review

**Spec coverage:**
- Always-include override → Task 4 (Steps 3, 7).
- Name-based matching with aliases (freshness) → Task 1 (`matches`) + Task 2 (alias capture).
- OpenAlex-id match path → Task 1 (`matches_openalex_author_ids`).
- JSON storage, atomic write, malformed-safe → Task 1 (`save`/`load`).
- Bot add/remove/list + ORCID + Scholar → Tasks 2, 3.
- Idempotency cursor + skip own messages → Task 3.
- Auto-pick + verification reply + `remove` safety net → Tasks 2, 3.
- Global scope, top-level `[author_whitelist]` → Task 4 (Step 8), Task 5.
- Cross-group dedup needs no extra mechanism → Task 4 (force-include into `passing`).
- `--dryrun` skips writes/replies → Task 3 (`dryrun`), Task 4 (Step 8 `save` guard).
- Config + README → Task 5.

**Type/name consistency:** `WhitelistedAuthor`, `AuthorWhitelist`, `normalize_author_name`,
`split_author_names`, `force_included_whitelist_items`, `AuthorResolveError`, `resolve`,
`parse_author_input`, `parse_command`, `run_author_whitelist_bot` are used consistently
across tasks. `resolve` is patched as `author_whitelist_bot.resolve` (imported into that
module) and `author_resolve.requests.get` in resolver tests — matches the import sites.

**Placeholder scan:** none — every code/test step contains complete code and exact commands.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-15-author-whitelist.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
