from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lock_experiment.py"
spec = importlib.util.spec_from_file_location("lock_experiment", SCRIPT)
assert spec and spec.loader
lock_experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lock_experiment)


class LockTests(unittest.TestCase):
    def test_modified_file_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("first", encoding="utf-8")
            files = {"a.txt": lock_experiment.digest(root / "a.txt")}
            aggregate = lock_experiment.hashlib.sha256(
                json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            lock = root / "lock.json"
            lock.write_text(
                json.dumps({"files": files, "aggregate_sha256": aggregate}), encoding="utf-8"
            )
            valid, errors = lock_experiment.verify_manifest(root, lock)
            self.assertTrue(valid, errors)
            (root / "a.txt").write_text("second", encoding="utf-8")
            valid, errors = lock_experiment.verify_manifest(root, lock)
            self.assertFalse(valid)
            self.assertIn("digest mismatch: a.txt", errors)


if __name__ == "__main__":
    unittest.main()
