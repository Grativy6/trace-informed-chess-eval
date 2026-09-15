"""Complete Inspect's required parameter metadata for the existing TIAI tool."""
from __future__ import annotations

import inspect


def describe_parameters(tool):
    """Derive descriptions from existing names without changing the callable."""
    names = list(inspect.signature(tool).parameters)
    tool.__doc__ = (tool.__doc__ or "").rstrip() + "\n\nArgs:\n" + "\n".join(
        f"    {name} (str): {name.replace('_', ' ')}." for name in names
    ) + "\n"
    return tool


def bind_repaired_factory(pair):
    """Reuse the frozen runner, changing only the returned tool's docstring."""
    original_loader = pair._runner
    def load():
        trial = original_loader()
        original_factory = trial.traced_bash_tool
        def factory(*args, **kwargs):
            return describe_parameters(original_factory(*args, **kwargs))
        trial.traced_bash_tool = factory
        return trial
    pair._runner = load
