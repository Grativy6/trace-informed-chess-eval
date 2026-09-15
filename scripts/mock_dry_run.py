#!/usr/bin/env python3
"""Exercise the complete generic gate without a provider or benchmark call."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tiai import (
    ActionGate,
    ActionProposal,
    Basis,
    ExecutionResult,
    KernelState,
    RelationToTask,
    TIAIKernel,
    TraceLedger,
    verify_trace,
)

TASK = "Use the calculator command to add two integers and report the result."
EXCERPT = "Use the calculator command"


def proposal(relation: RelationToTask, command: str) -> ActionProposal:
    basis = Basis.none("No basis asserted in this neutral mock.")
    return ActionProposal(
        observation="A calculator command is available.",
        observation_source="tool manifest",
        interpretation="It can perform the arithmetic named in the task.",
        candidate_action=f"Execute {command!r}.",
        relation_to_task=relation,
        recommendation="Record the declared relation before dispatch.",
        permission_basis=basis,
        authorization_basis=basis,
        uncertainty="",
        command=command,
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "trace.jsonl"
        state = KernelState.from_task_grant(TASK)
        kernel = TIAIKernel(state)
        ledger = TraceLedger(path, state.task_grant_sha256)
        ledger.append("session", {"mode": "mock", "task_grant": TASK})
        gate = ActionGate(
            kernel,
            ledger,
            lambda command: ExecutionResult(stdout="5\n" if command == "calc 2 3" else ""),
        )

        results = [gate.handle(proposal(relation, "calc 2 3")) for relation in RelationToTask]
        if not all(result.executed and result.decision_code == "receipt_bound" for result in results):
            raise SystemExit("semantic classifications did not share receipt-bound dispatch")

        verification = verify_trace(path, state.task_grant_sha256)
        if not verification.valid:
            raise SystemExit(f"trace failed verification: {verification.errors}")

        summary = {
            "semantic_decisions": [result.decision_code for result in results],
            "relations_retained": [relation.value for relation in RelationToTask],
            "records": verification.records,
            "final_hash": verification.final_hash,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
