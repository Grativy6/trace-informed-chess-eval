"""Host-only, atomic snapshots for a provider-credit episode.

The artifact is a recovery basis, not an Inspect continuation checkpoint.  It
does not execute commands, copy sandbox files, or imply that a saved state can
be resumed exactly after a provider or process failure.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping


class EpisodeCheckpointError(RuntimeError):
    """The checkpoint could not be serialized or durably written."""


_SECRET_KEY = re.compile(r"(?:api[_-]?key|credential|password|passwd|secret|authorization|bearer|token)", re.I)


def _jsonable(value: Any, *, redact_keys: bool = False) -> Any:
    """Convert public state objects without traversing host environment data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value)}
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"), redact_keys=redact_keys)
    if is_dataclass(value):
        return _jsonable(asdict(value), redact_keys=redact_keys)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if redact_keys and _SECRET_KEY.search(name):
                result[name] = "[redacted]"
            else:
                result[name] = _jsonable(item, redact_keys=redact_keys)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item, redact_keys=redact_keys) for item in value]
    # Preserve type information without repr(), which can expose credentials
    # embedded in arbitrary host objects.
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}


def _state_snapshot(state: Any) -> dict[str, Any]:
    if hasattr(state, "model_dump"):
        # Some downstream TaskState implementations are Pydantic models.
        dumped = state.model_dump(mode="json")
        # Only task metadata/store are treated as host-owned containers. Do
        # not redact message keys such as authorization_basis_*: those are
        # model-visible evidence and must remain complete in the artifact.
        if isinstance(dumped, Mapping):
            return {
                str(key): _jsonable(value, redact_keys=str(key) in {"metadata", "store"})
                for key, value in dumped.items()
            }
        return _jsonable(dumped)

    fields = (
        "model", "sample_id", "epoch", "input", "messages", "target", "choices",
        "output", "completed", "metadata", "store", "scores", "sample_uuid",
    )
    snapshot: dict[str, Any] = {}
    for field in fields:
        try:
            value = getattr(state, field)
        except AttributeError:
            continue
        # Metadata and Store are task-owned, so protect credential-shaped keys;
        # model input/output/messages remain complete and untouched.
        snapshot[field] = _jsonable(value, redact_keys=field in {"metadata", "store"})
    return snapshot


async def _sandbox_snapshot(sandbox_env: Any) -> dict[str, Any]:
    if sandbox_env is None:
        return {}
    connection = await sandbox_env.connection()
    result = {"connection": _jsonable(connection)}
    working_dir = getattr(sandbox_env, "working_dir", None)
    if working_dir is None:
        working_dir = getattr(sandbox_env, "_working_dir", None)
    if working_dir is not None:
        result["working_dir"] = _jsonable(working_dir)
    return result


def _controller_snapshot(controller: Any) -> dict[str, Any]:
    if controller is None:
        return {}
    burdens = getattr(controller, "open_burdens", {})
    if isinstance(burdens, Mapping):
        burdens_value = {
            str(key): _jsonable(item.to_dict() if hasattr(item, "to_dict") else item)
            for key, item in burdens.items()
        }
    else:
        burdens_value = _jsonable(burdens)
    result: dict[str, Any] = {"open_burdens": burdens_value}
    for field in ("action_admissions", "completed", "registry_hash", "canonical_registry_hash"):
        if hasattr(controller, field):
            result[field] = _jsonable(getattr(controller, field))
    registry = getattr(controller, "registry", None)
    if registry is not None and hasattr(registry, "registry_hash"):
        result["registry_hash"] = _jsonable(registry.registry_hash)
    return result


async def write_episode_checkpoint(
    path: Path,
    state: Any,
    sandbox_env: Any,
    controller: Any = None,
    reason: str = "episode_boundary",
) -> dict[str, Any]:
    """Write a durable, local snapshot and return the serialized payload.

    The temporary file is created beside ``path`` and atomically replaced only
    after JSON has been flushed and synced.  Existing checkpoints are replaced
    as a single filesystem operation; no provider or sandbox command is used.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = {
            "schema_version": "episode-checkpoint-1",
            "checkpoint_kind": "recovery_basis",
            "resume_claim": "not_an_exact_inspect_resume",
            "reason": reason,
            "state": _state_snapshot(state),
            "sandbox": await _sandbox_snapshot(sandbox_env),
            "controller": _controller_snapshot(controller),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            try:
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # File replacement is already durable enough on platforms
                # without syncable directory handles.
                pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return payload
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise EpisodeCheckpointError(f"episode checkpoint write failed: {destination}") from exc
