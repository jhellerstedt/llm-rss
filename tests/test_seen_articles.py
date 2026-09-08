import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from seen_articles import (
    SeenArticlesCorruptError,
    load_seen_articles,
    save_seen_articles,
    seen_articles_path,
)


class TestSeenArticles(unittest.TestCase):
    def test_path_next_to_config(self) -> None:
        p = Path("/tmp/config.d/config.toml")
        self.assertEqual(
            seen_articles_path(p),
            Path("/tmp/config.d/config.seen_articles.json"),
        )

    def test_missing_file_is_bootstrap(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "config.seen_articles.json"
            keys, bootstrap = load_seen_articles(path)
            self.assertTrue(bootstrap)
            self.assertEqual(keys, set())

    def test_empty_valid_file_is_not_bootstrap(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "config.seen_articles.json"
            path.write_text(
                json.dumps({"version": 1, "keys": []}), encoding="utf-8"
            )
            keys, bootstrap = load_seen_articles(path)
            self.assertFalse(bootstrap)
            self.assertEqual(keys, set())

    def test_corrupt_raises(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "config.seen_articles.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(SeenArticlesCorruptError):
                load_seen_articles(path)

    def test_roundtrip(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "config.seen_articles.json"
            save_seen_articles(path, {"doi:10.1/x", "arxiv:2609.02567"})
            keys, bootstrap = load_seen_articles(path)
            self.assertFalse(bootstrap)
            self.assertEqual(keys, {"doi:10.1/x", "arxiv:2609.02567"})

    def test_save_sorted_unique(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "config.seen_articles.json"
            save_seen_articles(path, {"b", "a", "a"})
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(doc["version"], 1)
            self.assertEqual(doc["keys"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
