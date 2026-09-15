from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tiai.spend import (
    CACHE_READ,
    CACHE_WRITE,
    MAX_OUTPUT_TOKENS,
    OUTPUT,
    SpendGovernor,
    SpendJournalError,
    SpendStopped,
)


class SpendGovernorTests(unittest.TestCase):
    def governor(self, directory: str) -> SpendGovernor:
        return SpendGovernor(Path(directory) / "spend.jsonl")

    def test_exact_rates_include_cache_reads_writes_and_output_once(self):
        with tempfile.TemporaryDirectory() as directory:
            governor = self.governor(directory)
            reservation = governor.reserve(100, 100)
            settlement = governor.settle(
                reservation,
                {"input_tokens": 100, "output_tokens": 10,
                 "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 30},
                 "output_tokens_details": {"reasoning_tokens": 7}},
                response_id="resp_1",
            )
            self.assertEqual(settlement["charged_kind"], "exact")
            self.assertEqual(settlement["charged_nanodollars"], 20 * CACHE_READ + 30 * CACHE_WRITE + 50 * 10_000 + 10 * OUTPUT)
            self.assertEqual(settlement["reasoning_tokens"], 7)

    def test_missing_cache_write_is_conservative_upper_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            governor = self.governor(directory)
            reservation = governor.reserve(50, 50)
            settlement = governor.settle(reservation, {"input_tokens": 50, "output_tokens": 1,
                "input_tokens_details": {"cached_tokens": 10}})
            self.assertEqual(settlement["charged_kind"], "conservative_upper_bound")
            self.assertEqual(settlement["charged_nanodollars"], 10 * CACHE_READ + 40 * CACHE_WRITE + OUTPUT)

    def test_long_context_pricing_and_output_multiplier(self):
        with tempfile.TemporaryDirectory() as directory:
            governor = self.governor(directory)
            reservation = governor.reserve(272_001, 20)
            settlement = governor.settle(reservation, {"input_tokens": 272_001, "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}})
            self.assertTrue(settlement["long_context"])
            self.assertEqual(settlement["charged_nanodollars"], 272_001 * 20_000 + 20 * 75_000)

    def test_final_output_ceiling_is_reduced_to_remaining_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            governor = self.governor(directory)
            reservation = governor.reserve(350_000, MAX_OUTPUT_TOKENS)
            self.assertLess(reservation.max_output_tokens, MAX_OUTPUT_TOKENS)
            self.assertGreaterEqual(reservation.max_output_tokens, 16)
            self.assertEqual(reservation.requested_max_output_tokens, MAX_OUTPUT_TOKENS)

    def test_huge_input_stops_without_numeric_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            governor = self.governor(directory)
            with self.assertRaisesRegex(SpendStopped, "budget_exhausted"):
                governor.reserve(10**30, 16)

    def test_malformed_usage_keeps_full_reservation_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            governor = self.governor(directory)
            reservation = governor.reserve(100, 100)
            outcome = governor.settle(reservation, {"input_tokens": "bad", "output_tokens": 1})
            self.assertEqual(outcome["status"], "uncertain")
            self.assertEqual(governor.summary()["unresolved_reserved_nanodollars"], reservation.reserved_nanodollars)
            with self.assertRaises(SpendStopped):
                governor.reserve(1, 16)

    def test_failed_journal_prevents_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            governor = self.governor(directory)
            with patch("tiai.spend.os.fsync", side_effect=OSError("disk")):
                with self.assertRaises(SpendJournalError):
                    governor.reserve(1, 16)
            self.assertEqual(governor.stop_reason, "journal_write_failure")
            with self.assertRaises(SpendStopped):
                governor.reserve(1, 16)

    def test_budget_cap_existing_journal_and_overage_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spend.jsonl"
            with self.assertRaisesRegex(Exception, "no more than 9.00"):
                SpendGovernor(path, "9.01")
            governor = SpendGovernor(path)
            reservation = governor.reserve(100, 16)
            settlement = governor.settle(reservation, {"input_tokens": 101, "output_tokens": 1,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}})
            self.assertTrue(settlement["pricing_bound_violation"])
            self.assertEqual(governor.stop_reason, "pricing_bound_violation")
            with self.assertRaises(SpendStopped):
                governor.reserve(1, 16)
            with self.assertRaisesRegex(Exception, "non-empty"):
                SpendGovernor(path)

    def test_provider_credit_mode_does_not_reduce_or_stop_on_local_dollars(self):
        with tempfile.TemporaryDirectory() as directory:
            governor = SpendGovernor(Path(directory) / "spend.jsonl", budget_usd=None)
            first = governor.reserve(350_000, MAX_OUTPUT_TOKENS)
            self.assertEqual(first.max_output_tokens, MAX_OUTPUT_TOKENS)
            self.assertEqual(governor.summary()["budget_nanodollars"], None)
            self.assertIsNone(governor.summary()["available_nanodollars"])
            self.assertEqual(governor.summary()["policy_identifier"], "provider_credit_uncapped")
            settled = governor.settle(
                first,
                {"input_tokens": 350_000, "output_tokens": 128_000,
                 "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}},
            )
            self.assertGreater(settled["charged_nanodollars"], 9 * 1_000_000_000)
            self.assertIsNone(governor.stop_reason)
            second = governor.reserve(100, 16)
            self.assertEqual(second.max_output_tokens, 16)

    def test_provider_credit_mode_keeps_uncertain_request_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            governor = SpendGovernor(Path(directory) / "spend.jsonl", budget_usd=None)
            reservation = governor.reserve(100, 16)
            outcome = governor.mark_uncertain(reservation, "provider_timeout")
            self.assertEqual(outcome["status"], "uncertain")
            self.assertEqual(governor.stop_reason, "usage_unreconciled")
            with self.assertRaises(SpendStopped):
                governor.reserve(1, 16)
