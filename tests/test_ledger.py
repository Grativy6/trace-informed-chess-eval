from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tiai.ledger import TraceLedger, verify_trace


class LedgerTests(unittest.TestCase):
    def test_hash_chain_and_append_only_correction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            grant_hash = "a" * 64
            ledger = TraceLedger(path, grant_hash)
            first = ledger.append("session", {"task": "mock"})
            second = ledger.append("environment_event", {"observation": "value"})
            correction = ledger.append_correction(
                second["record_hash"], {"observation": "corrected value"}, "clerical correction"
            )

            result = verify_trace(path, grant_hash)
            self.assertTrue(result.valid, result.errors)
            self.assertEqual(result.records, 3)
            self.assertEqual(result.final_hash, correction["record_hash"])

            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[0]["record_hash"], first["record_hash"])
            self.assertEqual(records[1]["payload"]["observation"], "value")
            self.assertEqual(records[2]["event_type"], "correction")

    def test_tamper_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            ledger = TraceLedger(path, "b" * 64)
            ledger.append("session", {"task": "mock"})
            path.write_text(path.read_text().replace("mock", "changed"), encoding="utf-8")
            result = verify_trace(path)
            self.assertFalse(result.valid)
            self.assertTrue(any("record hash mismatch" in error for error in result.errors))


if __name__ == "__main__":
    unittest.main()
