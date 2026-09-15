from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
import json
from contextlib import ExitStack
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("matched_pair", ROOT / "scripts" / "run_matched_pair.py")
assert SPEC and SPEC.loader
matched_pair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(matched_pair)


class MatchedPairTests(unittest.TestCase):
    def test_build_model_freezes_responses_standard_and_retries(self):
        captured = {}

        class FakeConfig:
            def __init__(self, **kwargs):
                captured["config"] = kwargs

        def fake_get_model(name, **kwargs):
            captured["name"] = name
            captured["kwargs"] = kwargs
            return object()

        with patch("inspect_ai.model.GenerateConfig", FakeConfig), patch("inspect_ai.model.get_model", fake_get_model):
            matched_pair.build_model("dummy")
        self.assertEqual(captured["name"], "openai/gpt-6-astra")
        self.assertEqual(captured["config"], {"max_retries": 0, "max_connections": 1})
        kwargs = dict(captured["kwargs"])
        config = kwargs.pop("config")
        self.assertEqual(kwargs, {"responses_api": True, "service_tier": "default", "memoize": False, "max_retries": 0, "api_key": "dummy"})
        self.assertEqual(config.__class__, FakeConfig)

    def test_preflight_requires_a_mode(self):
        with self.assertRaises(SystemExit):
            matched_pair.main([])

    def test_execute_requires_run_id_and_acknowledgement(self):
        with self.assertRaises(SystemExit):
            matched_pair.main(["--execute"])
        with self.assertRaises(SystemExit):
            matched_pair.main(["--execute", "--run-id", "x"])

    def test_control_solver_is_exact_upstream_solver(self):
        sentinel = object()
        upstream = SimpleNamespace(bounded_agent=lambda messages, seconds: sentinel)
        self.assertIs(matched_pair.control_solver(upstream, 200, 10800), sentinel)

    def test_limits_are_frozen(self):
        with self.assertRaises(SystemExit):
            matched_pair.main(["--preflight", "--message-limit", "199"])

    def test_budget_is_capped_at_nine(self):
        with self.assertRaises(SystemExit):
            matched_pair.main(["--preflight", "--budget-usd", "9.01"])

    def test_run_id_is_single_component(self):
        with self.assertRaises(SystemExit):
            matched_pair.main(["--execute", "--run-id", "a/b", "--acknowledge-external-cost"])

    def test_systemic_failure_is_distinguished(self):
        self.assertTrue(matched_pair._systemic_failure({"spend": {"stop_reason": "input_count_failure"}}))
        self.assertFalse(matched_pair._systemic_failure({"spend": {"stop_reason": "budget_exhausted_before_generation"}}))

    def _run_simulated_pair(self, folder, count_failure):
        bound = []
        def bind(model, governor):
            bound.append(governor)
        def run(arm, *args):
            if arm == "control":
                if count_failure:
                    bound[-1].record_count_failure("PermissionDeniedError")
                else:
                    from tiai.spend import SpendStopped
                    try:
                        bound[-1].reserve(10**9)
                    except SpendStopped:
                        pass
            return {"arm": arm, "status": "completed", "scores": []}
        argv = ["--execute", "--run-id", "single-use", "--budget-usd", "9.00",
                "--acknowledge-external-cost", "--output-dir", folder]
        with ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", {"OPENAI_API_KEY": "synthetic-unused"}))
            stack.enter_context(patch.object(matched_pair, "_validate", return_value=(object(), {}, "old-lock")))
            stack.enter_context(patch.object(matched_pair, "_pair_lock", return_value={"aggregate_sha256": "pair-lock"}))
            stack.enter_context(patch.object(matched_pair, "build_model", return_value=object()))
            stack.enter_context(patch("tiai.budgeted_openai.install_responses_budget", side_effect=bind))
            calls = stack.enter_context(patch.object(matched_pair, "run_arm", side_effect=run))
            stack.enter_context(patch("builtins.print"))
            rc = matched_pair.main(argv)
            with self.assertRaises(FileExistsError):
                matched_pair.main(argv)
        return rc, calls.call_count, json.loads((Path(folder)/"single-use/pair-summary.json").read_text())

    def test_caught_provider_limit_stops_pair_and_preserves_no_run_arm(self):
        with tempfile.TemporaryDirectory() as folder:
            rc, calls, summary = self._run_simulated_pair(folder, True)
            self.assertEqual((rc, calls), (1, 1))
            self.assertEqual(summary["arms"][0]["status"], "runtime_failure")
            self.assertEqual(summary["arms"][1]["status"], "prepared_not_run")
            self.assertEqual(summary["arms"][0]["spend"]["request_count"], 0)

    def test_budget_boundary_proceeds_to_independent_second_arm_once(self):
        with tempfile.TemporaryDirectory() as folder:
            rc, calls, summary = self._run_simulated_pair(folder, False)
            self.assertEqual((rc, calls), (0, 2))
            self.assertEqual(summary["arms"][0]["status"], "budget_limited")
            self.assertEqual(summary["arms"][1]["status"], "completed")
            self.assertIsNone(summary["arms"][1]["spend"]["stop_reason"])


if __name__ == "__main__":
    unittest.main()
