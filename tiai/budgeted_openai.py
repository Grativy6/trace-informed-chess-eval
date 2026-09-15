"""Instance-local Responses guard for the authorized Astra pair."""

from __future__ import annotations

import asyncio
from typing import Any

from openai import NotGiven
from inspect_ai.model._providers.openai_responses import HttpxHooks
from inspect_ai.util import LimitExceededError

from .spend import SpendGovernor

COUNT_FIELDS = {
    "model", "input", "instructions", "tools", "tool_choice", "text", "reasoning",
    "parallel_tool_calls", "previous_response_id", "conversation", "truncation",
    "personality",
}


class PaidEpisodeStop(LimitExceededError):
    def __init__(self, reason: str):
        # The monetary evidence lives in the governor, not this stop marker.
        super().__init__("custom", value=1, limit=1, message=reason)


def install_responses_budget(model: Any, governor: SpendGovernor) -> Any:
    """Wrap this model instance only; both SDK and Inspect retries must be zero."""
    if model.name != "gpt-6-astra" or not model.api.responses_api:
        raise ValueError("budget binding requires direct Astra Responses")
    if str(model.api.client.base_url).rstrip("/") != "https://api.openai.com/v1":
        raise ValueError("budget binding requires the official OpenAI API endpoint")
    # Cloning the SDK client keeps credentials in memory and disables SDK retries.
    client = model.api.client.with_options(max_retries=0)
    model.api.client = client
    original_create = client.responses.create
    dispatch_lock = asyncio.Lock()
    governor.record("provider_binding", {
        "model": "openai/gpt-6-astra", "service_tier": "default",
        "sdk_retries": client.max_retries, "inspect_retries": model.config.max_retries,
        "budget_nanodollars": governor.summary()["budget_nanodollars"],
        "policy_identifier": governor.summary()["policy_identifier"],
    })
    if model.config.max_retries != 0:
        raise ValueError("Inspect retries must be disabled before budget binding")

    async def guarded_create(**request: Any) -> Any:
        async with dispatch_lock:
            if governor.stop_reason:
                raise PaidEpisodeStop(governor.stop_reason)
            if request.get("model") != "gpt-6-astra":
                raise ValueError("unexpected model at dispatch")
            if request.get("stream") or request.get("background"):
                raise ValueError("budgeted pair requires nonstreaming foreground requests")
            if request.get("extra_body"):
                raise ValueError("unaccounted request overrides are not allowed")
            headers = request.get("extra_headers") or {}
            if set(headers) - {HttpxHooks.REQUEST_ID_HEADER}:
                raise ValueError("unexpected request header override")
            if request.get("service_tier", "default") not in ("default", None):
                raise ValueError("budgeted pair requires Standard service")
            supplied_tools = request.get("tools", [])
            if isinstance(supplied_tools, NotGiven):
                supplied_tools = []
            for tool in supplied_tools:
                if tool.get("type") != "function":
                    raise ValueError("only locally executed function tools are costed")
            request = dict(request)
            request["service_tier"] = "default"
            count_request = {
                key: value for key, value in request.items()
                if key in COUNT_FIELDS and not isinstance(value, NotGiven)
            }
            try:
                counted = await client.responses.input_tokens.count(**count_request)
                input_tokens = counted.input_tokens
            except BaseException as exc:
                governor.record_count_failure(type(exc).__name__)
                raise PaidEpisodeStop("input token counting failed; generation was not dispatched") from exc
            try:
                reservation = governor.reserve(input_tokens, request.get("max_output_tokens"))
            except Exception as exc:
                raise PaidEpisodeStop("budget could not reserve the next request") from exc
            request["max_output_tokens"] = reservation.max_output_tokens
            try:
                response = await original_create(**request)
            except BaseException as exc:
                governor.mark_uncertain(reservation, type(exc).__name__)
                raise PaidEpisodeStop("generation outcome unknown; reservation retained") from exc
            if response.model != "gpt-6-astra" and not response.model.startswith("gpt-6-astra-"):
                governor.mark_uncertain(reservation, "unexpected_response_model")
                raise PaidEpisodeStop("unexpected response model; pricing unresolved")
            if getattr(response, "service_tier", None) not in (None, "default"):
                governor.mark_uncertain(reservation, "unexpected_service_tier")
                raise PaidEpisodeStop("unexpected response service tier; pricing unresolved")
            usage = response.usage.model_dump() if response.usage is not None else {}
            try:
                governor.settle(
                    reservation, usage, response_id=response.id, response_model=response.model,
                    service_tier=getattr(response, "service_tier", None),
                )
            except Exception as exc:
                if not governor.stop_reason:
                    governor.mark_uncertain(reservation, type(exc).__name__)
                raise PaidEpisodeStop("response cost could not be reconciled") from exc
            if governor.stop_reason:
                raise PaidEpisodeStop(governor.stop_reason)
            return response

    client.responses.create = guarded_create
    return model
