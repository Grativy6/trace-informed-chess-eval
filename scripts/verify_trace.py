#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tiai.ledger import verify_trace


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace", type=Path)
    parser.add_argument("--task-grant-sha256")
    args = parser.parse_args()
    result = verify_trace(args.trace, args.task_grant_sha256)
    if not result.valid:
        for error in result.errors:
            print(error)
        raise SystemExit(1)
    print(f"valid trace: {result.records} records; final={result.final_hash}")


if __name__ == "__main__":
    main()
