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
            _resp(status=404),
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
            _resp({"results": [_OA_AUTHOR]}),
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
            ar.resolve(
                "https://scholar.google.com/citations?user=ABC", mailto="me@x.com"
            )


if __name__ == "__main__":
    unittest.main()
