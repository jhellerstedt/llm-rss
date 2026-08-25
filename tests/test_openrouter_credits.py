import math
import unittest
from unittest.mock import patch

import requests

from openrouter_credits import (
    OpenRouterInsufficientCredits,
    ensure_openrouter_credits_for_run,
    planned_openrouter_calls,
)


def _group(name: str, *, cap=None, batch=None, zulip=False) -> dict:
    g: dict = {
        "name": name,
        "prefilter_max_candidates": cap,
        "scoring_batch_size": batch,
        "zulip_sources": [{"realm": "r", "stream": "s"}] if zulip else [],
    }
    return g


class TestPlannedOpenRouterCalls(unittest.TestCase):
    def test_scoring_two_groups_cap20_batch10(self) -> None:
        groups = [
            _group("g1", cap=20, batch=10),
            _group("g2", cap=20, batch=10),
        ]
        n = planned_openrouter_calls(groups, ["scoring"], default_cap=20, default_batch=5)
        self.assertEqual(n, 4)

    def test_scoring_uses_kagi_defaults_when_group_unset(self) -> None:
        groups = [_group("g1")]
        # cap 20 / batch 5 → 4 scoring calls
        n = planned_openrouter_calls(groups, ["scoring"], default_cap=20, default_batch=5)
        self.assertEqual(n, 4)

    def test_cap_nonpositive_uses_50(self) -> None:
        groups = [_group("g1", cap=0, batch=10)]
        n = planned_openrouter_calls(groups, ["scoring"], default_cap=20, default_batch=5)
        self.assertEqual(n, math.ceil(50 / 10))

    def test_summarize_curate_domains_only_with_zulip(self) -> None:
        groups = [
            _group("with", zulip=True),
            _group("without", zulip=False),
        ]
        n = planned_openrouter_calls(
            groups, ["summarize", "curate", "domains"], default_cap=20, default_batch=5
        )
        # 1 summarize + 1 curate for zulip group, 1 domains for the run
        self.assertEqual(n, 3)

    def test_unrouted_types_are_zero(self) -> None:
        groups = [_group("g1", cap=20, batch=10, zulip=True)]
        self.assertEqual(planned_openrouter_calls(groups, [], default_cap=20, default_batch=5), 0)


def _http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(response=resp)


def _models_payload(model_id: str = "~anthropic/claude-haiku-latest") -> dict:
    return {
        "data": [
            {
                "id": model_id,
                "pricing": {
                    "prompt": "0.000001",
                    "completion": "0.000005",
                    "request": "0",
                },
            }
        ]
    }


class FakeClient:
    model = "~anthropic/claude-haiku-latest"
    max_tokens = 4096

    def __init__(self, by_path: dict[str, object]) -> None:
        self.by_path = by_path
        self.paths: list[str] = []

    def get_json(self, path: str) -> dict:
        self.paths.append(path)
        value = self.by_path[path]
        if isinstance(value, Exception):
            raise value
        return value  # type: ignore[return-value]


def _scoring_groups() -> list[dict]:
    return [
        _group("g1", cap=20, batch=10),
        _group("g2", cap=20, batch=10),
    ]


class TestEnsureOpenRouterCredits(unittest.TestCase):
    def test_aborts_when_key_remaining_below_estimate(self) -> None:
        client = FakeClient(
            {
                "/key": {"data": {"limit": 10, "limit_remaining": 0.01}},
                "/credits": _http_error(403),
                "/models": _models_payload(),
            }
        )
        with self.assertRaises(OpenRouterInsufficientCredits) as ctx:
            ensure_openrouter_credits_for_run(
                client,
                _scoring_groups(),
                ["scoring"],
                openrouter_table={},
                kagi_table={"prefilter_max_candidates": 20, "scoring_batch_size": 5},
            )
        msg = str(ctx.exception)
        self.assertIn("0.01", msg)
        self.assertIn("key", msg.lower())

    def test_proceeds_when_spendable_above_estimate(self) -> None:
        client = FakeClient(
            {
                "/key": {"data": {"limit": 10, "limit_remaining": 5.0}},
                "/credits": _http_error(403),
                "/models": _models_payload(),
            }
        )
        ensure_openrouter_credits_for_run(
            client,
            _scoring_groups(),
            ["scoring"],
            openrouter_table={},
            kagi_table={},
        )
        self.assertIn("/key", client.paths)

    def test_credits_403_uses_key_remaining(self) -> None:
        client = FakeClient(
            {
                "/key": {"data": {"limit": 10, "limit_remaining": 5.0}},
                "/credits": _http_error(403),
                "/models": _models_payload(),
            }
        )
        ensure_openrouter_credits_for_run(
            client, _scoring_groups(), ["scoring"], openrouter_table={}, kagi_table={}
        )

    def test_models_lookup_failure_aborts(self) -> None:
        client = FakeClient(
            {
                "/key": {"data": {"limit": 10, "limit_remaining": 5.0}},
                "/credits": _http_error(403),
                "/models": {"data": [{"id": "other/model", "pricing": {}}]},
            }
        )
        with self.assertRaises(OpenRouterInsufficientCredits):
            ensure_openrouter_credits_for_run(
                client, _scoring_groups(), ["scoring"], openrouter_table={}, kagi_table={}
            )

    def test_credit_check_false_skips_key_fetch(self) -> None:
        client = FakeClient({})
        ensure_openrouter_credits_for_run(
            client,
            _scoring_groups(),
            ["scoring"],
            openrouter_table={"credit_check": False},
            kagi_table={},
        )
        self.assertEqual(client.paths, [])

    def test_unlimited_key_without_account_aborts(self) -> None:
        client = FakeClient(
            {
                "/key": {"data": {"limit": None, "limit_remaining": None}},
                "/credits": _http_error(403),
                "/models": _models_payload(),
            }
        )
        with self.assertRaises(OpenRouterInsufficientCredits):
            ensure_openrouter_credits_for_run(
                client, _scoring_groups(), ["scoring"], openrouter_table={}, kagi_table={}
            )

    def test_n_calls_zero_skips_money_check(self) -> None:
        client = FakeClient({})
        ensure_openrouter_credits_for_run(
            client,
            _scoring_groups(),
            [],
            openrouter_table={},
            kagi_table={},
        )
        self.assertEqual(client.paths, [])


class TestChatMaxTokens(unittest.TestCase):
    def test_chat_completion_sends_max_tokens(self) -> None:
        from openrouter_client import OpenRouterClient

        captured: dict = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["json"] = json
            resp = requests.Response()
            resp.status_code = 200
            resp._content = (
                b'{"choices":[{"message":{"content":"ok"}}],"usage":'
                b'{"prompt_tokens":1,"completion_tokens":1}}'
            )
            resp.headers["Content-Type"] = "application/json"
            return resp

        client = OpenRouterClient(api_key="sk-test", max_tokens=4096)
        with patch("openrouter_client.requests.post", side_effect=fake_post):
            out = client.chat_completion([{"role": "user", "content": "hi"}])
        self.assertEqual(out, "ok")
        self.assertEqual(captured["json"]["max_tokens"], 4096)

    def test_get_json_records_openrouter_http(self) -> None:
        from api_usage import get_api_usage_snapshot, reset_api_usage_stats
        from openrouter_client import OpenRouterClient

        def fake_get(url, headers=None, timeout=None):
            self.assertTrue(str(url).endswith("/key"))
            resp = requests.Response()
            resp.status_code = 200
            resp._content = b'{"data":{"limit_remaining":1}}'
            return resp

        reset_api_usage_stats()
        client = OpenRouterClient(api_key="sk-test")
        with patch("openrouter_client.requests.get", side_effect=fake_get):
            doc = client.get_json("/key")
        self.assertEqual(doc["data"]["limit_remaining"], 1)
        self.assertEqual(get_api_usage_snapshot()["openrouter"], 1)

    def test_pricing_matches_model_id_without_tilde(self) -> None:
        client = FakeClient(
            {
                "/key": {"data": {"limit": 10, "limit_remaining": 5.0}},
                "/credits": _http_error(403),
                "/models": _models_payload("anthropic/claude-haiku-latest"),
            }
        )
        ensure_openrouter_credits_for_run(
            client, _scoring_groups(), ["scoring"], openrouter_table={}, kagi_table={}
        )



if __name__ == "__main__":
    unittest.main()

