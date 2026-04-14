import logging
import unittest

from api_usage import (
    log_api_usage_summary,
    record_kagi_fastgpt_http,
    record_openalex_http,
    reset_api_usage_stats,
)


class TestApiUsage(unittest.TestCase):
    def test_reset_and_increment(self) -> None:
        reset_api_usage_stats()
        record_kagi_fastgpt_http(2)
        record_openalex_http(1)
        log = logging.getLogger("test_api_usage")
        with self.assertLogs(log, level="INFO") as cm:
            log_api_usage_summary(log)
        self.assertTrue(any("total_calls=3" in m for m in cm.output))
        self.assertTrue(any("Kagi_FastGPT_http=2" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main()
