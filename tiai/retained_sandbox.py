"""Attach to an already-running Inspect Docker sandbox.

This is deliberately an attachment-only adapter.  It never starts, stops,
recreates, or cleans up a container.  The caller must supply the exact
container and Compose project recorded by the paid episode.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from inspect_ai.util._sandbox.environment import SandboxEnvironment, SandboxEnvironmentConfigType
from inspect_ai.util._sandbox.registry import sandboxenv
from inspect_ai.util._sandbox.docker.docker import DockerSandboxEnvironment
from inspect_ai.util._sandbox.docker.util import ComposeProject


_DOCKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class RetainedSandboxSpec:
    """Exact attachment coordinates persisted by the interrupted episode."""

    project_name: str
    compose_file: Path
    container_name: str
    service: str = "default"
    working_dir: str = "/workdir"

    def validate_shape(self) -> None:
        for label, value in (
            ("project_name", self.project_name),
            ("container_name", self.container_name),
            ("service", self.service),
        ):
            if not value or not _DOCKER_NAME.fullmatch(value):
                raise ValueError(f"invalid Docker {label}: {value!r}")
        if not self.compose_file.is_file():
            raise FileNotFoundError(self.compose_file)
        if not self.working_dir.startswith("/"):
            raise ValueError("working_dir must be an absolute container path")


def inspect_retained_container(spec: RetainedSandboxSpec) -> dict[str, Any]:
    """Read and validate exact container identity without changing Docker state."""

    spec.validate_shape()
    completed = subprocess.run(
        ["docker", "inspect", "--format", "{{json .}}", spec.container_name],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "docker inspect failed")
    try:
        record = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("docker inspect returned invalid JSON") from exc
    if not isinstance(record, dict):
        raise RuntimeError("docker inspect returned an unexpected shape")

    actual_name = str(record.get("Name", "")).lstrip("/")
    if actual_name != spec.container_name:
        raise RuntimeError(f"container name mismatch: {actual_name!r}")
    labels = record.get("Config", {}).get("Labels", {})
    if labels.get("com.docker.compose.project") != spec.project_name:
        raise RuntimeError("container is not in the recorded Compose project")
    if labels.get("com.docker.compose.service") != spec.service:
        raise RuntimeError("container service does not match the recorded service")
    if record.get("State", {}).get("Running") is not True:
        raise RuntimeError("retained container is not running")
    return {
        "container_name": actual_name,
        "project_name": spec.project_name,
        "service": spec.service,
        "image": record.get("Config", {}).get("Image"),
        "state": record.get("State"),
    }


class RetainedDockerSandbox(DockerSandboxEnvironment):
    """A Docker sandbox whose lifecycle is owned by an earlier paid run."""

    def __init__(self, spec: RetainedSandboxSpec, *, validate: bool = True) -> None:
        if validate:
            self.identity = inspect_retained_container(spec)
        else:
            spec.validate_shape()
            self.identity = {
                "container_name": spec.container_name,
                "project_name": spec.project_name,
                "service": spec.service,
            }
        project = ComposeProject(
            name=spec.project_name,
            config=str(spec.compose_file.resolve()),
            sample_id=None,
            epoch=None,
            env=None,
        )
        # This constructor only stores project/service/working-directory state;
        # unlike DockerSandboxEnvironment.sample_init it does not run Compose.
        super().__init__(spec.service, project, spec.working_dir)
        self.retained_spec = spec


def attach_retained_sandbox(
    spec: RetainedSandboxSpec, *, validate: bool = True
) -> RetainedDockerSandbox:
    """Return an attachment-only sandbox for the exact recorded container."""

    return RetainedDockerSandbox(spec, validate=validate)


def spec_from_config(config: SandboxEnvironmentConfigType | None) -> RetainedSandboxSpec:
    """Load a retained attachment spec from a JSON config path or mapping."""

    if config is None:
        raise ValueError("retained_docker requires a JSON attachment spec")
    if isinstance(config, str):
        path = Path(config).resolve()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"cannot read retained sandbox spec: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"retained sandbox spec is not JSON: {path}") from exc
    elif isinstance(config, dict):
        value = config
    else:
        value = config.model_dump(mode="json")
    if not isinstance(value, dict):
        raise ValueError("retained sandbox spec must be a JSON object")
    required = {"project_name", "compose_file", "container_name"}
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError("retained sandbox spec missing: " + ", ".join(missing))
    return RetainedSandboxSpec(
        project_name=str(value["project_name"]),
        compose_file=Path(str(value["compose_file"])).resolve(),
        container_name=str(value["container_name"]),
        service=str(value.get("service", "default")),
        working_dir=str(value.get("working_dir", "/workdir")),
    )


@sandboxenv(name="retained_docker")
class RetainedDockerProvider(SandboxEnvironment):
    """Inspect provider that attaches, but never owns, an existing container."""

    @classmethod
    async def task_init(
        cls, task_name: str, config: SandboxEnvironmentConfigType | None
    ) -> None:
        # Deliberately no Docker calls: attachment happens in sample_init.
        spec_from_config(config).validate_shape()

    @classmethod
    async def task_init_environment(
        cls, config: SandboxEnvironmentConfigType | None, metadata: dict[str, str]
    ) -> dict[str, str]:
        spec_from_config(config).validate_shape()
        return {}

    @classmethod
    async def sample_init(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        metadata: dict[str, str],
    ) -> dict[str, SandboxEnvironment]:
        spec = spec_from_config(config)
        return {"default": attach_retained_sandbox(spec)}

    @classmethod
    async def sample_cleanup(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        environments: dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        # The paid episode owns this container. Never stop or remove it here.
        return None

    @classmethod
    async def task_cleanup(
        cls, task_name: str, config: SandboxEnvironmentConfigType | None, cleanup: bool
    ) -> None:
        # The paid episode owns this container. Never stop or remove it here.
        return None
