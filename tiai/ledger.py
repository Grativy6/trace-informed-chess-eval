"""Append-only, hash-chained JSONL trace receipts."""
from __future__ import annotations
import json, os
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable
SCHEMA_VERSION = "0.2"
_SUPPORTED_SCHEMA_VERSIONS = {"0.1", SCHEMA_VERSION}
class TracePersistenceError(RuntimeError): pass
def _canonical(value: dict[str, Any]) -> bytes: return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
@dataclass(frozen=True)
class TraceVerification:
    valid: bool; records: int; final_hash: str | None; errors: tuple[str, ...]
class TraceLedger:
    def __init__(self, path: Path, task_grant_sha256: str):
        self.path, self.task_grant_sha256, self._poisoned = Path(path), task_grant_sha256, False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size:
            check = verify_trace(self.path, expected_task_grant_sha256=task_grant_sha256)
            if not check.valid: raise ValueError(f"cannot append to invalid trace: {check.errors}")
            self._sequence, self._previous_hash = check.records, check.final_hash
        else: self._sequence, self._previous_hash = 0, None
    @property
    def poisoned(self) -> bool: return self._poisoned
    def append(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._poisoned: raise TracePersistenceError("ledger is poisoned after an uncertain persistence failure")
        sequence = self._sequence + 1
        base = {"schema_version": SCHEMA_VERSION, "sequence": sequence, "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "event_type": event_type, "task_grant_sha256": self.task_grant_sha256, "previous_record_hash": self._previous_hash, "payload": payload}
        try:
            record_hash = sha256(_canonical(base)).hexdigest(); record = {**base, "record_hash": record_hash}; self._persist(record)
        except BaseException as exc:
            self._poisoned = True
            if isinstance(exc, TracePersistenceError): raise
            raise TracePersistenceError("trace receipt was not durably persisted") from exc
        self._sequence, self._previous_hash = sequence, record_hash
        return record
    def _persist(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"); handle.flush(); os.fsync(handle.fileno())
    def append_correction(self, corrects_record_hash: str, replacement: dict[str, Any], reason: str) -> dict[str, Any]:
        return self.append("correction", {"corrects_record_hash": corrects_record_hash, "replacement": replacement, "reason": reason})
def _read_records(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip(): continue
            try: value = json.loads(raw)
            except json.JSONDecodeError as exc: yield line_number, {"__decode_error__": str(exc)}; continue
            if not isinstance(value, dict):
                yield line_number, {"__record_error__": "record must be a JSON object"}
                continue
            yield line_number, value
def verify_trace(path: Path, expected_task_grant_sha256: str | None = None) -> TraceVerification:
    errors: list[str] = []; previous_hash: str | None = None; count = 0
    for line_number, record in _read_records(Path(path)):
        count += 1
        if "__decode_error__" in record: errors.append(f"line {line_number}: {record['__decode_error__']}"); continue
        if "__record_error__" in record: errors.append(f"line {line_number}: {record['__record_error__']}"); continue
        if record.get("schema_version") not in _SUPPORTED_SCHEMA_VERSIONS: errors.append(f"line {line_number}: unsupported schema version")
        if record.get("sequence") != count: errors.append(f"line {line_number}: expected sequence {count}")
        if record.get("previous_record_hash") != previous_hash: errors.append(f"line {line_number}: previous hash mismatch")
        if expected_task_grant_sha256 and record.get("task_grant_sha256") != expected_task_grant_sha256: errors.append(f"line {line_number}: task grant hash mismatch")
        supplied_hash = record.get("record_hash"); base = {key: value for key, value in record.items() if key != "record_hash"}
        if supplied_hash != sha256(_canonical(base)).hexdigest(): errors.append(f"line {line_number}: record hash mismatch")
        previous_hash = supplied_hash
    return TraceVerification(not errors, count, previous_hash, tuple(errors))
