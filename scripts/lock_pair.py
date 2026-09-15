#!/usr/bin/env python3
"""Freeze and verify the matched pair without changing the preserved v0.2 lock."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import lock_experiment

PAIR_LOCK = ROOT / "MATCHED_PAIR_LOCK.json"
EXTRA_PATHS = (
    "EXPERIMENT_LOCK.json", "MATCHED_PAIR_CONTRACT.md",
    "tiai/spend.py", "tiai/budgeted_openai.py",
    "scripts/run_matched_pair.py", "scripts/lock_pair.py",
)
EXPECTED_IMAGE_ID = "sha256:9fe9c6b333d940a4682e9ec6eafe282169b3e4d17b4fc1179767370a98680b00"


def _files():
    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
            for p in sorted(set(lock_experiment.LOCKED_PATHS) | set(EXTRA_PATHS))}


def _aggregate(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_pair_lock():
    valid, errors = lock_experiment.verify_manifest()
    if not valid:
        raise RuntimeError("preserved v0.2 lock is invalid: " + "; ".join(errors))
    files = _files()
    return {
        "version": "matched-pair-0.1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": files, "aggregate_sha256": _aggregate(files),
        "upstream_commit": json.loads((ROOT / "UPSTREAM.lock.json").read_text())["commit"],
        "docker_image_id": EXPECTED_IMAGE_ID,
        "model": "openai/gpt-6-astra", "service_tier": "default",
        "per_arm_budget_usd": "9.00", "combined_budget_usd": "18.00",
        "arms": ["control", "tiai"], "epochs_per_arm": 1,
        "runtime_packages": {p: importlib.metadata.version(p) for p in ("inspect-ai", "openai")},
    }


def verify_pair_lock():
    stored = json.loads(PAIR_LOCK.read_text())
    current = _files()
    if current != stored["files"] or _aggregate(current) != stored["aggregate_sha256"]:
        raise RuntimeError("matched pair code lock mismatch")
    valid, errors = lock_experiment.verify_manifest()
    if not valid:
        raise RuntimeError("preserved v0.2 lock mismatch: " + "; ".join(errors))
    for package, expected in stored["runtime_packages"].items():
        if importlib.metadata.version(package) != expected:
            raise RuntimeError("matched pair dependency mismatch: " + package)
    upstream = ROOT / "upstream" / "beat-stockfish"
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=upstream, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=upstream, text=True).strip()
    if head != stored["upstream_commit"] or dirty:
        raise RuntimeError("matched pair upstream source mismatch")
    images = json.loads(subprocess.check_output(["docker", "image", "inspect", "beat-stockfish:local"], text=True))
    if images[0]["Id"] != stored["docker_image_id"]:
        raise RuntimeError("matched pair Docker image mismatch")
    return stored


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.write:
        data = build_pair_lock()
        PAIR_LOCK.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    else:
        data = verify_pair_lock()
    print(json.dumps({"status": "PAIR_LOCK_WRITTEN" if args.write else "PAIR_LOCK_VERIFIED",
                      "aggregate_sha256": data["aggregate_sha256"]}))


if __name__ == "__main__":
    main()
