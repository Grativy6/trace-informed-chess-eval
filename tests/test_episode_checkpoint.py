from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import unittest

from tiai.episode_checkpoint import EpisodeCheckpointError, write_episode_checkpoint


@dataclass
class Message:
    role: str
    content: str


@dataclass
class State:
    model: str
    sample_id: str
    epoch: int
    input: str
    messages: list[Message]
    completed: bool
    metadata: dict
    store: dict


class Sandbox:
    async def connection(self):
        return {"type": "mock", "command": "connect", "container": "test-container"}

    _working_dir = "/workdir"


class Controller:
    registry = type("Registry", (), {"registry_hash": "registry-hash"})()
    open_burdens = {"b1": type("Burden", (), {"to_dict": lambda self: {"id": "b1", "status": "open"}})()}
    action_admissions = [{"status": "admitted", "action": "local_compute"}]
    completed = False


class EpisodeCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_roundtrip_preserves_model_content_and_visible_context(self):
        state = State(
            model="openai/gpt-6-astra", sample_id="sample-1", epoch=1,
            input="play the game", messages=[Message("user", "Your move."), Message("assistant", "e2e4")],
            completed=False, metadata={"phase": "play"}, store={"api_key": "do-not-copy", "counter": 2},
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "episode.json"
            payload = await write_episode_checkpoint(path, state, Sandbox(), Controller(), reason="provider_stop")
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded, payload)
            self.assertEqual(loaded["state"]["messages"][1]["content"], "e2e4")
            self.assertEqual(loaded["sandbox"]["working_dir"], "/workdir")
            self.assertEqual(loaded["controller"]["open_burdens"]["b1"]["status"], "open")
            self.assertEqual(loaded["controller"]["registry_hash"], "registry-hash")
            self.assertEqual(loaded["state"]["store"]["api_key"], "[redacted]")
            self.assertEqual(loaded["resume_claim"], "not_an_exact_inspect_resume")

    async def test_replacement_is_atomic_and_no_temporary_file_remains(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "episode.json"
            path.write_text('{"old":true}', encoding="utf-8")
            await write_episode_checkpoint(path, State("model", "id", 1, "in", [], False, {}, {}), Sandbox())
            self.assertNotEqual(path.read_text(encoding="utf-8"), '{"old":true}')
            self.assertEqual(list(Path(folder).glob(".*.tmp")), [])

    async def test_model_dump_keeps_message_authorization_fields(self):
        class DumpState:
            def model_dump(self, mode="json"):
                return {
                    "messages": [{"authorization_basis_rationale": "exact task text"}],
                    "metadata": {"api_key": "redact-me"},
                    "store": {},
                }

        with tempfile.TemporaryDirectory() as folder:
            payload = await write_episode_checkpoint(Path(folder) / "episode.json", DumpState(), Sandbox())
            self.assertEqual(
                payload["state"]["messages"][0]["authorization_basis_rationale"],
                "exact task text",
            )
            self.assertEqual(payload["state"]["metadata"]["api_key"], "[redacted]")

    async def test_sandbox_failure_surfaces_and_does_not_create_artifact(self):
        class BrokenSandbox:
            async def connection(self):
                raise RuntimeError("sandbox unavailable")

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "episode.json"
            with self.assertRaises(EpisodeCheckpointError):
                await write_episode_checkpoint(path, State("model", "id", 1, "in", [], False, {}, {}), BrokenSandbox())
            self.assertFalse(path.exists())

    async def test_cancellation_is_not_hidden(self):
        class CancelledSandbox:
            async def connection(self):
                raise asyncio.CancelledError()

        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(asyncio.CancelledError):
                await write_episode_checkpoint(Path(folder) / "episode.json", State("model", "id", 1, "in", [], False, {}, {}), CancelledSandbox())


if __name__ == "__main__":
    unittest.main()
