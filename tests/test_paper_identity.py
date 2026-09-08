import unittest

from openalex_enrich import PaperEnrichment
from paper_identity import cluster_links_by_identity, identity_keys


ACS_HTML = (
    "https://pubs.acs.org/nalefd/article/doi/10.1021/acs.nanolett.6c03195/"
    "5404181/Direct-Observation-of-the-Zigzag-Edge-States-of-a"
)
ACS_DX = "http://dx.doi.org/10.1021/acs.nanolett.6c03195"
ARXIV = "https://arxiv.org/abs/2609.02567"
DATACITE = "https://doi.org/10.48550/arXiv.2609.02567"


class TestIdentityKeys(unittest.TestCase):
    def test_acs_html_url(self) -> None:
        keys = identity_keys(ACS_HTML)
        self.assertIn("doi:10.1021/acs.nanolett.6c03195", keys)
        self.assertTrue(any(k.startswith("url:") for k in keys))

    def test_dx_doi_url(self) -> None:
        keys = identity_keys(ACS_DX)
        self.assertIn("doi:10.1021/acs.nanolett.6c03195", keys)

    def test_arxiv_abs(self) -> None:
        keys = identity_keys(ARXIV)
        self.assertIn("arxiv:2609.02567", keys)

    def test_datacite_arxiv_doi(self) -> None:
        keys = identity_keys(DATACITE)
        self.assertIn("arxiv:2609.02567", keys)

    def test_enrichment_arxiv_and_doi(self) -> None:
        en = PaperEnrichment(
            top_author_name="A",
            first_affiliation="X",
            last_affiliation="Y",
            arxiv_url=ARXIV,
            doi="10.1021/acs.nanolett.6c03195",
        )
        keys = identity_keys("https://pubs.acs.org/doi/10.1021/acs.nanolett.6c03195", en)
        self.assertIn("arxiv:2609.02567", keys)
        self.assertIn("doi:10.1021/acs.nanolett.6c03195", keys)


class TestClusterLinks(unittest.TestCase):
    def test_journal_and_arxiv_share_doi(self) -> None:
        journal = "https://dx.doi.org/10.1021/acs.nanolett.6c03195"
        arxiv = ARXIV
        en = PaperEnrichment(
            top_author_name="A",
            first_affiliation="X",
            last_affiliation="Y",
            doi="10.1021/acs.nanolett.6c03195",
        )
        clusters = cluster_links_by_identity(
            {
                journal: identity_keys(journal),
                arxiv: identity_keys(arxiv, en),
            }
        )
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]), 2)

    def test_unrelated_links_stay_apart(self) -> None:
        a = "https://arxiv.org/abs/2401.00001"
        b = "https://arxiv.org/abs/2401.00002"
        clusters = cluster_links_by_identity(
            {a: identity_keys(a), b: identity_keys(b)}
        )
        self.assertEqual(len(clusters), 2)


if __name__ == "__main__":
    unittest.main()
