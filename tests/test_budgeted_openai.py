from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import httpx
from inspect_ai.model import GenerateConfig, get_model

from tiai.budgeted_openai import PaidEpisodeStop, install_responses_budget
from tiai.spend import SpendGovernor


def response_body(usage=True):
    return {
        "id": "resp_test", "object": "response", "created_at": 1,
        "model": "gpt-6-astra", "status": "completed", "service_tier": "default",
        "output": [{"type": "message", "id": "msg_test", "status": "completed",
                    "role": "assistant", "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
        "usage": {"input_tokens": 123, "input_tokens_details": {
            "cached_tokens": 20, "cache_write_tokens": 3,
        }, "output_tokens": 8, "output_tokens_details": {"reasoning_tokens": 3},
            "total_tokens": 131} if usage else None,
    }


class BudgetedOpenAITests(unittest.IsolatedAsyncioTestCase):
    async def test_real_inspect_sdk_path_counts_and_reserves_before_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / "spend.jsonl"
            governor = SpendGovernor(journal)
            requests = []

            def handler(request):
                payload = json.loads(request.content)
                requests.append((request.url.path, payload))
                if request.url.path.endswith("/input_tokens"):
                    self.assertEqual(payload["model"], "gpt-6-astra")
                    self.assertNotIn("max_output_tokens", payload)
                    return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 123})
                entries = [json.loads(line) for line in journal.read_text().splitlines()]
                self.assertEqual(entries[-1]["event"], "reservation")
                self.assertEqual(payload["max_output_tokens"], 128000)
                self.assertEqual(payload["service_tier"], "default")
                return httpx.Response(200, json=response_body())

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                model = get_model(
                    "openai/gpt-6-astra", api_key="dummy", responses_api=True, memoize=False,
                    max_retries=0, http_client=http_client, service_tier="default",
                    config=GenerateConfig(max_retries=0, max_connections=1),
                )
                install_responses_budget(model, governor)
                result = await model.generate("Say ok.")
                self.assertEqual(result.completion, "ok")
                self.assertEqual(len(requests), 2)
                self.assertEqual(governor.summary()["reconciled_nanodollars"], 1_457_500)
                self.assertEqual(model.api.client.max_retries, 0)

    async def test_unknown_generation_charge_is_not_retried_or_released(self):
        with tempfile.TemporaryDirectory() as folder:
            governor = SpendGovernor(Path(folder) / "spend.jsonl")
            generation_calls = 0

            def handler(request):
                nonlocal generation_calls
                if request.url.path.endswith("/input_tokens"):
                    return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 123})
                generation_calls += 1
                raise httpx.ReadTimeout("synthetic timeout", request=request)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                model = get_model(
                    "openai/gpt-6-astra", api_key="dummy", responses_api=True, memoize=False,
                    max_retries=0, http_client=http_client,
                    config=GenerateConfig(max_retries=0, max_connections=1),
                )
                install_responses_budget(model, governor)
                for _ in range(2):
                    with self.assertRaises(PaidEpisodeStop):
                        await model.generate("Say ok.")
                self.assertEqual(generation_calls, 1)
                self.assertGreater(governor.summary()["unresolved_reserved_nanodollars"], 0)

    async def test_count_failure_never_dispatches_generation_and_stays_terminal(self):
        with tempfile.TemporaryDirectory() as folder:
            governor = SpendGovernor(Path(folder) / "spend.jsonl")
            requests = []
            def handler(request):
                requests.append(request.url.path)
                return httpx.Response(401, json={"error": {"message": "synthetic", "type": "invalid_request_error"}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                model = get_model(
                    "openai/gpt-6-astra", api_key="dummy", responses_api=True, memoize=False,
                    max_retries=0, http_client=http_client,
                    config=GenerateConfig(max_retries=0, max_connections=1),
                )
                install_responses_budget(model, governor)
                for _ in range(2):
                    with self.assertRaises(PaidEpisodeStop):
                        await model.generate("Say ok.")
                self.assertEqual(requests, ["/v1/responses/input_tokens"])
                self.assertEqual(governor.summary()["request_count"], 0)


if __name__ == "__main__":
    unittest.main()
