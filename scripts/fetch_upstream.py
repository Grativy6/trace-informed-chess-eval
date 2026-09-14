#!/usr/bin/env python3
"""Fetch and verify the pinned Goodhart upstream checkout."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "UPSTREAM.lock.json").read_text(encoding="utf-8"))
DEST = ROOT / "upstream" / "beat-stockfish"


def run(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True)
    return completed.stdout.strip()


def main() -> None:
    if shutil.which("git") is None:
        raise SystemExit("git is required")
    DEST.parent.mkdir(parents=True, exist_ok=True)
    if not (DEST / ".git").exists():
        if DEST.exists():
            raise SystemExit(f"destination exists but is not a git checkout: {DEST}")
        run("git", "clone", "--filter=blob:none", LOCK["repository"], str(DEST))
    origin = run("git", "remote", "get-url", "origin", cwd=DEST)
    if origin.rstrip("/") != LOCK["repository"].rstrip("/"):
        raise SystemExit(f"unexpected origin: {origin}")
    run("git", "fetch", "origin", LOCK["commit"], cwd=DEST)
    run("git", "checkout", "--detach", LOCK["commit"], cwd=DEST)
    actual = run("git", "rev-parse", "HEAD", cwd=DEST)
    if actual != LOCK["commit"]:
        raise SystemExit(f"upstream SHA mismatch: expected {LOCK['commit']}, got {actual}")
    print(f"verified upstream {actual} at {DEST}")


if __name__ == "__main__":
    main()
