from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("run_tiai_trial", ROOT / "scripts" / "run_tiai_trial.py")
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class ProviderTransportTests(unittest.TestCase):
    def test_responses_flag_merges_into_provider_model_args(self) -> None:
        args = runner.build_model_args(
            "openai/gpt-6-astra",
            no_reasoning=False,
            responses_api=True,
            reasoning_args=lambda model: {"model_args": {"reasoning_enabled": True}},
        )
        self.assertEqual(
            args,
            {"model_args": {"reasoning_enabled": True, "responses_api": True}},
        )

    def test_responses_flag_rejects_other_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "only for direct openai"):
            runner.build_model_args(
                "openrouter/anthropic/claude-fable-5.1",
                no_reasoning=False,
                responses_api=True,
                reasoning_args=lambda model: {},
            )

    def test_local_construction_does_not_generate(self) -> None:
        with (
            patch("inspect_ai.model.Model.generate", side_effect=AssertionError("model generation attempted")),
            patch("socket.socket.connect", side_effect=AssertionError("network connection attempted")),
        ):
            model = runner.construct_preflight_model(
                "openai/gpt-6-astra", {"model_args": {"responses_api": True}}
            )
        self.assertEqual(runner.provider_transport(model), "responses")

    def test_generation_settings_are_not_provider_arguments(self) -> None:
        with patch("inspect_ai.model.get_model") as construct:
            runner.construct_preflight_model(
                "openai/gpt-6-astra",
                {"reasoning_effort": "high", "model_args": {"responses_api": True}},
            )
        construct.assert_called_once_with(
            "openai/gpt-6-astra", api_key="tiai-runtime-preflight-dummy",
            memoize=False, responses_api=True,
        )

    def test_direct_astra_requires_responses_flag(self) -> None:
        with self.assertRaisesRegex(ValueError, "require --responses-api"):
            runner.build_model_args(
                "openai/gpt-6-astra",
                no_reasoning=False,
                responses_api=False,
                reasoning_args=lambda model: {},
            )


if __name__ == "__main__":
    unittest.main()
