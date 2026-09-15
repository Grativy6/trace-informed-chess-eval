from __future__ import annotations
import asyncio, json, tempfile, unittest
from pathlib import Path
from tiai import ActionGate, ActionProposal, Basis, ExecutionResult, KernelState, RelationToTask, TIAIKernel, TraceLedger
from tiai.ledger import TracePersistenceError
def proposal(relation: RelationToTask | str, basis: Basis | None = None):
    chosen = basis if basis is not None else Basis.none()
    return ActionProposal("", "", "", "", relation, "", chosen, chosen, "", "neutral command")
class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        state = KernelState.from_task_grant("fixed task grant")
        self.ledger = TraceLedger(Path(self.temp.name) / "trace.jsonl", state.task_grant_sha256); self.calls=[]
        self.gate = ActionGate(TIAIKernel(state), self.ledger, lambda cmd: self.calls.append(cmd) or ExecutionResult("ok"))
    def test_every_semantic_relation_dispatches_after_receipts(self):
        for relation in RelationToTask:
            with self.subTest(relation=relation): self.assertTrue(self.gate.handle(proposal(relation)).executed)
        self.assertEqual(self.calls, ["neutral command"] * 3)
        events=[json.loads(x)["event_type"] for x in self.ledger.path.read_text().splitlines()]
        self.assertEqual(events, ["proposed", "decision", "consequence"] * 3)
    def test_invalid_trace_never_dispatches_but_is_receipted(self):
        result=self.gate.handle(proposal("other")); self.assertFalse(result.executed); self.assertEqual(self.calls, [])
    def test_sync_async_parity_and_error_link(self):
        sync=self.gate.handle(proposal(RelationToTask.WITHIN_TASK))
        async def run(cmd): return ExecutionResult("ok")
        async_result=asyncio.run(self.gate.handle_async(proposal(RelationToTask.OUTSIDE_TASK), run))
        self.assertEqual((sync.executed,sync.decision_code),(async_result.executed,async_result.decision_code))
        def broken(cmd): raise LookupError("boom")
        bad=ActionGate(self.gate.kernel,self.ledger,broken)
        with self.assertRaises(LookupError): bad.handle(proposal(RelationToTask.UNRESOLVED))
        records=[json.loads(x) for x in self.ledger.path.read_text().splitlines()]
        self.assertEqual(records[-1]["payload"]["status"],"executor_error")
        self.assertEqual(records[-1]["payload"]["decision_record_hash"],records[-2]["record_hash"])
    def test_persistence_failure_prevents_executor_and_poisoned_ledger_stays_closed(self):
        def fail(record): raise OSError("disk failure")
        self.ledger._persist=fail
        with self.assertRaises(TracePersistenceError): self.gate.handle(proposal(RelationToTask.WITHIN_TASK))
        self.assertEqual(self.calls, []); self.assertTrue(self.ledger.poisoned)
        with self.assertRaises(TracePersistenceError): self.gate.handle(proposal(RelationToTask.WITHIN_TASK))
    def test_mismatched_grant_rejected_at_construction(self):
        other=TraceLedger(Path(self.temp.name)/"other.jsonl", "a"*64)
        with self.assertRaises(ValueError): ActionGate(self.gate.kernel, other)
