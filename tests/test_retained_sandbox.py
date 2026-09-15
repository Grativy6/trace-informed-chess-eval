import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tiai.retained_sandbox import (
    RetainedDockerProvider,
    RetainedSandboxSpec,
    attach_retained_sandbox,
    inspect_retained_container,
    spec_from_config,
)


class RetainedSandboxTests(unittest.TestCase):
    def spec(self, folder: str) -> RetainedSandboxSpec:
        compose = Path(folder) / "compose.json"
        compose.write_text("{}", encoding="utf-8")
        return RetainedSandboxSpec(
            project_name="inspect-task-abc123",
            compose_file=compose,
            container_name="inspect-task-abc123-default-1",
        )

    def docker_result(self, spec: RetainedSandboxSpec) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["docker"],
            0,
            json.dumps(
                {
                    "Name": "/" + spec.container_name,
                    "Config": {
                        "Image": "beat-stockfish:local",
                        "Labels": {
                            "com.docker.compose.project": spec.project_name,
                            "com.docker.compose.service": spec.service,
                        },
                    },
                    "State": {"Running": True},
                }
            ),
            "",
        )

    def test_validation_is_read_only_exact_and_shell_free(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            spec = self.spec(folder)
            with patch(
                "tiai.retained_sandbox.subprocess.run",
                return_value=self.docker_result(spec),
            ) as run:
                identity = inspect_retained_container(spec)
            self.assertEqual(identity["container_name"], spec.container_name)
            args, kwargs = run.call_args
            self.assertEqual(args[0][-1], spec.container_name)
            self.assertFalse(kwargs.get("shell", False))
            self.assertTrue(kwargs["capture_output"])

    def test_mismatched_compose_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            spec = self.spec(folder)
            result = self.docker_result(spec)
            payload = json.loads(result.stdout)
            payload["Config"]["Labels"]["com.docker.compose.project"] = "other"
            result.stdout = json.dumps(payload)
            with patch("tiai.retained_sandbox.subprocess.run", return_value=result):
                with self.assertRaisesRegex(RuntimeError, "Compose project"):
                    inspect_retained_container(spec)

    def test_attachment_does_not_start_container(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            spec = self.spec(folder)
            with patch(
                "tiai.retained_sandbox.subprocess.run",
                return_value=self.docker_result(spec),
            ) as run:
                sandbox = attach_retained_sandbox(spec)
            self.assertEqual(sandbox.retained_spec.container_name, spec.container_name)
            # Construction performs only inspect; lifecycle methods such as
            # compose up/down are never called by this adapter.
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][:3], ["docker", "inspect", "--format"])

    def test_invalid_name_is_rejected_before_docker(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            spec = self.spec(folder)
            invalid = RetainedSandboxSpec(
                project_name=spec.project_name,
                compose_file=spec.compose_file,
                container_name="bad name; rm -rf /",
            )
            with patch("tiai.retained_sandbox.subprocess.run") as run:
                with self.assertRaises(ValueError):
                    attach_retained_sandbox(invalid)
            run.assert_not_called()

    def test_registered_provider_only_attaches_and_never_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            spec = self.spec(folder)
            config = Path(folder) / "attachment.json"
            config.write_text(
                json.dumps(
                    {
                        "project_name": spec.project_name,
                        "compose_file": str(spec.compose_file),
                        "container_name": spec.container_name,
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "tiai.retained_sandbox.subprocess.run",
                return_value=self.docker_result(spec),
            ):
                environments = __import__("asyncio").run(
                    RetainedDockerProvider.sample_init("continuation", str(config), {})
                )
                __import__("asyncio").run(
                    RetainedDockerProvider.sample_cleanup(
                        "continuation", str(config), environments, True
                    )
                )
                __import__("asyncio").run(
                    RetainedDockerProvider.task_cleanup("continuation", str(config), True)
                )
            self.assertEqual(environments["default"].retained_spec.container_name, spec.container_name)

    def test_spec_loader_requires_exact_fields(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "compose.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "container_name"):
                spec_from_config(
                    {
                        "project_name": "inspect-task-abc123",
                        "compose_file": str(path),
                    }
                )


if __name__ == "__main__":
    unittest.main()
