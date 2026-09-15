"""Optional, narrow recovery for transient provider 5xx Responses failures.

This wrapper sits outside the spend governor.  One logical generation keeps
one governor reservation; failed provider attempts are journaled as separate
events, with usage explicitly marked unknown, before a bounded retry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from openai import InternalServerError


@dataclass
class ProviderRecoveryState:
    """Safe metadata that a continuation summary can carry forward."""

    max_retries: int = 2
    attempts: int = 0
    failed_attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_retries": self.max_retries,
            "attempts": self.attempts,
            "failed_attempts": [dict(item) for item in self.failed_attempts],
            "usage_reconciled_for_failed_attempts": False if self.failed_attempts else True,
        }


def _status_code(error: BaseException) -> int | None:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _request_id(error: BaseException) -> str | None:
    value = getattr(error, "request_id", None)
    if isinstance(value, str) and value:
        return value
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        value = headers.get("x-request-id") or headers.get("request-id")
        if isinstance(value, str) and value:
            return value
    return None


def install_provider_error_recovery(
    client: Any,
    governor: Any,
    *,
    max_retries: int = 2,
    base_delay_seconds: float = 0.5,
) -> ProviderRecoveryState:
    """Wrap ``client.responses.create`` with bounded HTTP-5xx recovery.

    Install this before ``install_responses_budget``.  The budget wrapper then
    owns the single reservation and settlement for the logical generation,
    while this wrapper only retries the provider call inside that reservation.
    """
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("max_retries must be a non-negative integer")
    if base_delay_seconds < 0:
        raise ValueError("base_delay_seconds must be non-negative")
    original_create = client.responses.create
    state = ProviderRecoveryState(max_retries=max_retries)

    async def recovered_create(**request: Any) -> Any:
        for retry_index in range(max_retries + 1):
            state.attempts += 1
            try:
                return await original_create(**request)
            except BaseException as exc:
                status_code = _status_code(exc)
                failure = {
                    "attempt": state.attempts,
                    "retry_index": retry_index,
                    "exception_class": type(exc).__name__,
                    "status_code": status_code,
                    "request_id": _request_id(exc),
                    "usage_reconciled": False,
                    "usage_status": "missing_after_failed_attempt",
                }
                state.failed_attempts.append(failure)
                governor.record("provider_generation_failure", failure)
                governor.record(
                    "provider_generation_usage_missing",
                    {
                        "attempt": state.attempts,
                        "status_code": status_code,
                        "request_id": failure["request_id"],
                        "usage_reconciled": False,
                        "reason": "failed_provider_attempt_has_no_response_usage",
                    },
                )
                retryable = isinstance(exc, InternalServerError) and status_code is not None and 500 <= status_code <= 599
                if not retryable or retry_index >= max_retries:
                    raise
                if base_delay_seconds:
                    await asyncio.sleep(base_delay_seconds * (2**retry_index))
        raise RuntimeError("provider recovery loop ended unexpectedly")

    client.responses.create = recovered_create
    return state

