#!/usr/bin/env python3
"""Write or verify the pre-run experiment lock."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "EXPERIMENT_LOCK.json"
LOCKED_PATHS = (
    "EXPERIMENT_CONTRACT.md",
    "TIAI_KERNEL_v0.1.md",
    "TRACE_SCHEMA.json",
    "SCORING_RUBRIC.md",
    "UPSTREAM.lock.json",
    "tiai/kernel.py",
    "tiai/ledger.py",
    "tiai/intervention.py",
    "scripts/run_tiai_trial.py",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(root: Path = ROOT) -> dict[str, object]:
    files = {relative: digest(root / relative) for relative in LOCKED_PATHS}
    aggregate = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "lock_version": "0.1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "files": files,
        "aggregate_sha256": aggregate,
    }


def verify_manifest(root: Path = ROOT, lock_path: Path = LOCK_PATH) -> tuple[bool, list[str]]:
    if not lock_path.exists():
        return False, [f"missing lock: {lock_path}"]
    stored = json.loads(lock_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    for relative, expected in stored.get("files", {}).items():
        path = root / relative
        if not path.exists():
            errors.append(f"missing locked file: {relative}")
        elif digest(path) != expected:
            errors.append(f"digest mismatch: {relative}")
    current_files = stored.get("files", {})
    aggregate = hashlib.sha256(
        json.dumps(current_files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if aggregate != stored.get("aggregate_sha256"):
        errors.append("aggregate digest mismatch")
    return not errors, errors


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    if args.write:
        manifest = build_manifest()
        LOCK_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {LOCK_PATH} ({manifest['aggregate_sha256']})")
        return

    valid, errors = verify_manifest()
    if not valid:
        for error in errors:
            print(error)
        raise SystemExit(1)
    manifest = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    print(f"verified {LOCK_PATH} ({manifest['aggregate_sha256']})")


if __name__ == "__main__":
    main()
