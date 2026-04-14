import unittest

from kagi_quota import (
    MAX_KAGI_INVOCATIONS_PER_RUN,
    MAX_OPENALEX_FALLBACK_FASTGPT_PER_RUN,
    KagiOpenAlexFallbackQuotaExceeded,
    KagiSessionQuotaExceeded,
    consume_kagi_invocation,
    plan_scoring_budget,
    reset_kagi_session_quota,
)


class TestKagiQuota(unittest.TestCase):
    def setUp(self) -> None:
        reset_kagi_session_quota()

    def test_plan_scoring_budget_respects_reserve(self) -> None:
        # 70 - 7 reserve = 63 scoring budget "slots" (batches)
        sl, bs = plan_scoring_budget(200, prefilter_cap=20, batch_size=5)
        self.assertEqual(bs, 5)
        self.assertLessEqual(sl, 20)
        self.assertLessEqual((sl + bs - 1) // bs, 63)

    def test_total_cap(self) -> None:
        for _ in range(MAX_KAGI_INVOCATIONS_PER_RUN):
            consume_kagi_invocation(kind="fastgpt")
        with self.assertRaises(KagiSessionQuotaExceeded):
            consume_kagi_invocation(kind="summarize")

    def test_openalex_fallback_cap_independent_of_kind(self) -> None:
        for _ in range(MAX_OPENALEX_FALLBACK_FASTGPT_PER_RUN):
            consume_kagi_invocation(kind="fastgpt", openalex_fallback=True)
        with self.assertRaises(KagiOpenAlexFallbackQuotaExceeded):
            consume_kagi_invocation(kind="fastgpt", openalex_fallback=True)

    def test_fallback_bucket_after_non_fallback_calls(self) -> None:
        for _ in range(5):
            consume_kagi_invocation(kind="fastgpt", openalex_fallback=False)
        for _ in range(5):
            consume_kagi_invocation(kind="fastgpt", openalex_fallback=True)
        with self.assertRaises(KagiOpenAlexFallbackQuotaExceeded):
            consume_kagi_invocation(kind="fastgpt", openalex_fallback=True)


if __name__ == "__main__":
    unittest.main()
