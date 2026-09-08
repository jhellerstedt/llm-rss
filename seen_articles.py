"""Persistent seen-set of paper identity keys beside the feed config."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SEEN_VERSION = 1


class SeenArticlesCorruptError(Exception):
    """Seen file exists but cannot be parsed; do not bootstrap."""


def seen_articles_path(config_path: Path) -> Path:
    return config_path.with_name(f"{config_path.stem}.seen_articles.json")


def load_seen_articles(path: Path) -> tuple[set[str], bool]:
    """Return (keys, bootstrap). Missing file → bootstrap True. Corrupt → raise."""
    if not path.is_file():
        return set(), True
    try:
        raw = path.read_text(encoding="utf-8")
        doc = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        raise SeenArticlesCorruptError(f"unreadable seen file {path}: {e}") from e
    if not isinstance(doc, dict) or doc.get("version") != SEEN_VERSION:
        raise SeenArticlesCorruptError(f"invalid seen file {path}")
    keys_raw = doc.get("keys")
    if not isinstance(keys_raw, list):
        raise SeenArticlesCorruptError(f"invalid seen keys in {path}")
    keys = {str(k) for k in keys_raw if str(k).strip()}
    return keys, False


def save_seen_articles(path: Path, keys: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(keys)
    payload = {"version": SEEN_VERSION, "keys": ordered}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote %d seen article key(s) to %s", len(ordered), path)
