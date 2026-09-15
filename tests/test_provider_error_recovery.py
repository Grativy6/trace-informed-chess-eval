from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import httpx
from openai import APIStatusError, InternalServerError
from inspect_ai.model import GenerateConfig, get_model

from tiai.budgeted_openai import install_responses_budget
from tiai.provider_error_recovery import install_provider_error_recovery
from tiai.spend import SpendGovernor


def provider_error(error_type, status: int, request_id: str = "req-test"):
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(status, request=request, headers={"x-request-id": request_id})
    return error_type("synthetic", response=response, body={"error": {"message": "synthetic"}})


class Governor:
    def __init__(self):
        self.events = []

    def record(self, event, payload):
        self.events.append((event, payload))


class ProviderErrorRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_5xx_failures_then_success(self):
        governor = Governor()
        calls = 0

        async def create(**request):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise provider_error(InternalServerError, 500, f"req-{calls}")
            return {"ok": True}

        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        state = install_provider_error_recovery(client, governor, base_delay_seconds=0)
        result = await client.responses.create(api_key="must-not-be-journaled")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, 3)
        self.assertEqual(state.attempts, 3)
        self.assertEqual(len(state.failed_attempts), 2)
        self.assertFalse(state.to_dict()["usage_reconciled_for_failed_attempts"])
        self.assertEqual(governor.events[0][0], "provider_generation_failure")
        serialized = json.dumps(governor.events)
        self.assertNotIn("must-not-be-journaled", serialized)
        self.assertNotIn("api_key", serialized)

    async def test_4xx_is_not_retried(self):
        governor = Governor()
        calls = 0

        async def create(**request):
            nonlocal calls
            calls += 1
            raise provider_error(APIStatusError, 429)

        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        install_provider_error_recovery(client, governor, base_delay_seconds=0)
        with self.assertRaises(APIStatusError):
            await client.responses.create()
        self.assertEqual(calls, 1)
        self.assertEqual(len(governor.events), 2)

    async def test_exhaustion_propagates_last_5xx_after_two_retries(self):
        governor = Governor()
        calls = 0

        async def create(**request):
            nonlocal calls
            calls += 1
            raise provider_error(InternalServerError, 503, f"req-{calls}")

        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        state = install_provider_error_recovery(client, governor, base_delay_seconds=0)
        with self.assertRaises(InternalServerError):
            await client.responses.create()
        self.assertEqual(calls, 3)
        self.assertEqual(state.attempts, 3)
        self.assertEqual(len(state.failed_attempts), 3)

    async def test_unknown_transport_error_is_not_retried(self):
        governor = Governor()
        calls = 0

        async def create(**request):
            nonlocal calls
            calls += 1
            raise TimeoutError("synthetic")

        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        install_provider_error_recovery(client, governor, base_delay_seconds=0)
        with self.assertRaises(TimeoutError):
            await client.responses.create()
        self.assertEqual(calls, 1)

    async def test_budget_binding_preserves_recovery_on_cloned_client(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / "spend.jsonl"
            governor = SpendGovernor(journal)
            requests = []

            def handler(request):
                requests.append(request.url.path)
                if request.url.path.endswith("/input_tokens"):
                    return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 123})
                if len([path for path in requests if path.endswith("/responses")]) == 1:
                    return httpx.Response(
                        500,
                        request=request,
                        headers={"x-request-id": "req-transient"},
                        json={"error": {"message": "temporary"}},
                    )
                return httpx.Response(200, json={
                    "id": "resp_recovered", "object": "response", "created_at": 1,
                    "model": "gpt-6-astra", "status": "completed", "service_tier": "default",
                    "output": [{"type": "message", "id": "msg_recovered", "status": "completed",
                                "role": "assistant", "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
                    "usage": {"input_tokens": 123, "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 3},
                              "output_tokens": 8, "output_tokens_details": {"reasoning_tokens": 3}, "total_tokens": 131},
                })

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                model = get_model(
                    "openai/gpt-6-astra", api_key="dummy", responses_api=True, memoize=False,
                    max_retries=0, http_client=http_client, service_tier="default",
                    config=GenerateConfig(max_retries=0, max_connections=1),
                )
                recovery = None

                def prepare(client):
                    nonlocal recovery
                    recovery = install_provider_error_recovery(client, governor, base_delay_seconds=0)

                install_responses_budget(model, governor, prepare_client=prepare)
                result = await model.generate("Say ok.")
                self.assertEqual(result.completion, "ok")
                self.assertEqual(requests.count("/v1/responses"), 2)
                self.assertEqual(requests.count("/v1/responses/input_tokens"), 1)
                self.assertEqual(governor.summary()["request_count"], 1)
                self.assertEqual(len([line for line in journal.read_text().splitlines() if '"event":"settlement"' in line]), 1)
                self.assertIsNotNone(recovery)
                self.assertEqual(recovery.attempts, 2)


if __name__ == "__main__":
    unittest.main()
