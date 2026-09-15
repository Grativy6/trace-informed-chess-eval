from __future__ import annotations
import json, tempfile, unittest
from pathlib import Path
from tiai.ledger import TraceLedger, verify_trace
class LedgerTests(unittest.TestCase):
    def test_hash_chain_durable_append_and_correction(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"trace.jsonl"; ledger=TraceLedger(path,"a"*64)
            first=ledger.append("session",{"task":"mock"}); second=ledger.append("environment_event",{"observation":"value"}); correction=ledger.append_correction(second["record_hash"],{"observation":"corrected"},"clerical")
            result=verify_trace(path,"a"*64); self.assertTrue(result.valid,result.errors); self.assertEqual(result.records,3); self.assertEqual(result.final_hash,correction["record_hash"]); self.assertEqual(first["schema_version"],"0.2")
    def test_tamper_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"trace.jsonl"; ledger=TraceLedger(path,"b"*64); ledger.append("session",{"task":"mock"}); path.write_text(path.read_text().replace("mock","changed"),encoding="utf-8")
            self.assertFalse(verify_trace(path).valid)
