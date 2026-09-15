"""Host-only, append-only spending guard for one bounded provider run.

The governor deliberately treats a request with unknown usage as fully spent.
It is not a billing system: its job is to prevent this runner from making a
second paid request after it can no longer account for the first one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping


NANODOLLARS_PER_DOLLAR = 1_000_000_000
MAX_BUDGET_NANODOLLARS = 9 * NANODOLLARS_PER_DOLLAR
MAX_OUTPUT_TOKENS = 128_000
MIN_AFFORDABLE_OUTPUT_TOKENS = 16
LONG_CONTEXT_TOKENS = 272_000

# Standard-tier USD per million tokens, expressed as integer nanodollars/token.
UNCACHED_INPUT = 10_000
CACHE_READ = 1_000
CACHE_WRITE = 12_500
OUTPUT = 50_000


class SpendGovernorError(RuntimeError):
    """A local accounting or journal error."""


class SpendStopped(SpendGovernorError):
    """The governor has reached a terminal state and will not dispatch."""


class SpendJournalError(SpendGovernorError):
    """The durable journal could not be written."""


@dataclass(frozen=True)
class Reservation:
    request_index: int
    input_tokens: int
    requested_max_output_tokens: int
    max_output_tokens: int
    reserved_nanodollars: int
    input_reservation_nanodollars: int
    output_reservation_nanodollars: int
    long_context: bool


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


class SpendGovernor:
    """Reserve conservatively before dispatch, then settle one response at a time.

    ``path`` is intentionally single-use. A non-empty path is refused so a
    restarted command cannot accidentally resume, replay, or reset a paid run.
    """

    def __init__(self, path: Path, budget_usd: str | None = "9.00") -> None:
        self.path = Path(path)
        # ``None`` is an explicit provider-credit mode.  It still journals
        # conservative reservations and exact settlements, but the provider
        # account (rather than a local dollar ceiling) decides when a request
        # can no longer be made.
        self.policy_identifier = "provider_credit_uncapped" if budget_usd is None else "local_usd_cap"
        self.budget_nanodollars = None if budget_usd is None else self._parse_budget(budget_usd)
        if self.path.exists() and self.path.stat().st_size:
            raise SpendGovernorError(f"refusing non-empty spend journal: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sequence = 0
        self._previous_hash: str | None = None
        self._reservations: dict[int, Reservation] = {}
        self._settlements: dict[int, dict[str, Any]] = {}
        self._inflight: int | None = None
        self._reconciled_nanodollars = 0
        self._unresolved_nanodollars = 0
        self.stop_reason: str | None = None
        self._poisoned = False

    @staticmethod
    def _parse_budget(budget_usd: str) -> int:
        try:
            whole, fraction = budget_usd.split(".")
            if not whole.isdigit() or len(fraction) != 2 or not fraction.isdigit():
                raise ValueError
            parsed = int(whole) * NANODOLLARS_PER_DOLLAR + int(fraction) * 10_000_000
        except (AttributeError, ValueError):
            raise SpendGovernorError("budget_usd must have exactly two decimal places") from None
        if parsed <= 0 or parsed > MAX_BUDGET_NANODOLLARS:
            raise SpendGovernorError("budget_usd must be greater than zero and no more than 9.00")
        return parsed

    @staticmethod
    def _rates(input_tokens: int) -> tuple[int, int, int, bool]:
        long_context = input_tokens > LONG_CONTEXT_TOKENS
        input_multiplier = 2 if long_context else 1
        output_multiplier = 3 if long_context else 2
        # Output is multiplied by 1.5 in a long context; keep all math integral.
        output_rate = OUTPUT * output_multiplier // 2
        return UNCACHED_INPUT * input_multiplier, CACHE_READ * input_multiplier, CACHE_WRITE * input_multiplier, long_context

    @staticmethod
    def _output_rate(long_context: bool) -> int:
        return OUTPUT * 3 // 2 if long_context else OUTPUT

    def _append(self, event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._poisoned:
            raise SpendStopped(self.stop_reason or "journal_write_failure")
        record = {
            "schema_version": "spend-1",
            "sequence": self._sequence + 1,
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "previous_hash": self._previous_hash,
            "payload": dict(payload),
        }
        record["record_hash"] = hashlib.sha256(_canonical(record)).hexdigest()
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            self._poisoned = True
            self.stop_reason = "journal_write_failure"
            raise SpendJournalError("spend journal persistence failed") from exc
        self._sequence = record["sequence"]
        self._previous_hash = record["record_hash"]
        return record

    def record(self, event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Append a safe host-side audit event; callers must omit secrets/raw errors."""
        if not isinstance(event, str) or not event:
            raise SpendGovernorError("event must be non-empty")
        return self._append(event, payload)

    def record_count_failure(self, error_type: str) -> dict[str, Any]:
        if not isinstance(error_type, str) or not error_type:
            raise SpendGovernorError("error_type must be non-empty")
        self.stop_reason = f"input_count_failure:{error_type}"
        return self._append("input_count_failure", {"error_type": error_type})

    def reserve(
        self, input_tokens: int, requested_max_output_tokens: int | None = None
    ) -> Reservation:
        """Durably reserve a request before it can be sent to the provider."""
        if self.stop_reason:
            raise SpendStopped(self.stop_reason)
        if self._inflight is not None:
            raise SpendGovernorError("a request is already inflight")
        input_tokens = _int(input_tokens, "input_tokens")
        desired_output = MAX_OUTPUT_TOKENS if requested_max_output_tokens is None else _int(
            requested_max_output_tokens, "requested_max_output_tokens"
        )
        desired_output = min(desired_output, MAX_OUTPUT_TOKENS)
        _, _, worst_input_rate, long_context = self._rates(input_tokens)
        input_reservation = input_tokens * worst_input_rate
        output_rate = self._output_rate(long_context)
        if self.budget_nanodollars is None:
            available = None
            output_ceiling = desired_output
        else:
            available = self.budget_nanodollars - self._reconciled_nanodollars - self._unresolved_nanodollars
            affordable_output = max(0, (available - input_reservation) // output_rate)
            output_ceiling = min(desired_output, affordable_output)
            if output_ceiling < MIN_AFFORDABLE_OUTPUT_TOKENS:
                self.stop_reason = "budget_exhausted_before_generation"
                self._append(
                    "budget_stop",
                    {"input_tokens": input_tokens, "available_nanodollars": max(0, available), "reason": self.stop_reason},
                )
                raise SpendStopped(self.stop_reason)
        reservation = Reservation(
            request_index=len(self._reservations) + 1,
            input_tokens=input_tokens,
            requested_max_output_tokens=desired_output,
            max_output_tokens=output_ceiling,
            reserved_nanodollars=input_reservation + output_ceiling * output_rate,
            input_reservation_nanodollars=input_reservation,
            output_reservation_nanodollars=output_ceiling * output_rate,
            long_context=long_context,
        )
        self._append(
            "reservation",
            {
                "reservation": asdict(reservation),
                "output_ceiling_reduced": output_ceiling < desired_output,
                "policy_identifier": self.policy_identifier,
            },
        )
        self._reservations[reservation.request_index] = reservation
        self._inflight = reservation.request_index
        self._unresolved_nanodollars += reservation.reserved_nanodollars
        return reservation

    @staticmethod
    def _usage_details(usage: Mapping[str, Any]) -> tuple[int, int, int | None, int]:
        if not isinstance(usage, Mapping):
            raise ValueError("usage must be a mapping")
        input_tokens = _int(usage.get("input_tokens"), "input_tokens")
        output_tokens = _int(usage.get("output_tokens"), "output_tokens")
        details = usage.get("input_tokens_details", {})
        if details is None:
            details = {}
        if not isinstance(details, Mapping):
            raise ValueError("input_tokens_details must be a mapping")
        cached = _int(details.get("cached_tokens", usage.get("cached_tokens", 0)), "cached_tokens")
        write_raw = details.get("cache_write_tokens", usage.get("cache_write_tokens"))
        writes = None if write_raw is None else _int(write_raw, "cache_write_tokens")
        if cached > input_tokens or writes is not None and cached + writes > input_tokens:
            raise ValueError("cached and cache-write tokens exceed input tokens")
        return input_tokens, cached, writes, output_tokens

    def mark_uncertain(self, reservation: Reservation, error_type: str) -> dict[str, Any]:
        """Retain the full reservation after an uncertain dispatched request."""
        self._check_inflight(reservation)
        if not isinstance(error_type, str) or not error_type:
            raise SpendGovernorError("error_type must be non-empty")
        outcome = {"request_index": reservation.request_index, "status": "uncertain", "error_type": error_type,
                   "reserved_nanodollars": reservation.reserved_nanodollars}
        self._append("uncertain", outcome)
        self._settlements[reservation.request_index] = outcome
        self._inflight = None
        self.stop_reason = "usage_unreconciled"
        return outcome

    def _check_inflight(self, reservation: Reservation) -> None:
        if not isinstance(reservation, Reservation) or self._inflight != reservation.request_index:
            raise SpendGovernorError("reservation is not the current inflight request")
        if self._reservations.get(reservation.request_index) != reservation:
            raise SpendGovernorError("unknown reservation")

    def settle(
        self,
        reservation: Reservation,
        usage: dict[str, Any],
        response_id: str | None = None,
        response_model: str | None = None,
        service_tier: str | None = None,
    ) -> dict[str, Any]:
        """Settle a response; malformed usage consumes the reservation and stops."""
        self._check_inflight(reservation)
        try:
            input_tokens, cached_tokens, cache_write_tokens, output_tokens = self._usage_details(usage)
        except (TypeError, ValueError):
            return self.mark_uncertain(reservation, "malformed_usage")
        uncached_rate, cached_rate, write_rate, long_context = self._rates(input_tokens)
        output_rate = self._output_rate(long_context)
        conservative = cache_write_tokens is None
        if conservative:
            # Cache writes are not exposed by every Responses usage record. Charging all
            # non-cache-read input at the write rate is a safe upper bound.
            input_charge = cached_tokens * cached_rate + (input_tokens - cached_tokens) * write_rate
        else:
            normal_tokens = input_tokens - cached_tokens - cache_write_tokens
            input_charge = (
                cached_tokens * cached_rate
                + cache_write_tokens * write_rate
                + normal_tokens * uncached_rate
            )
        charged = input_charge + output_tokens * output_rate
        exceeds = input_tokens > reservation.input_tokens or output_tokens > reservation.max_output_tokens
        exceeds = exceeds or charged > reservation.reserved_nanodollars
        settlement = {
            "request_index": reservation.request_index,
            "status": "settled",
            "response_id": response_id,
            "response_model": response_model,
            "service_tier": service_tier,
            "input_tokens": input_tokens,
            "cached_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": usage.get("output_tokens_details", {}).get("reasoning_tokens")
            if isinstance(usage.get("output_tokens_details"), Mapping)
            else None,
            "charged_nanodollars": charged,
            "charged_kind": "conservative_upper_bound" if conservative else "exact",
            "reserved_nanodollars": reservation.reserved_nanodollars,
            "long_context": long_context,
            "pricing_bound_violation": exceeds,
        }
        self._append("settlement", settlement)
        self._settlements[reservation.request_index] = settlement
        self._inflight = None
        self._unresolved_nanodollars -= reservation.reserved_nanodollars
        self._reconciled_nanodollars += charged
        if exceeds:
            self.stop_reason = "pricing_bound_violation"
        return settlement

    def summary(self) -> dict[str, Any]:
        available = None if self.budget_nanodollars is None else max(
            0, self.budget_nanodollars - self._reconciled_nanodollars - self._unresolved_nanodollars
        )
        conservative = sum(
            settlement["charged_nanodollars"]
            for settlement in self._settlements.values()
            if settlement.get("charged_kind") == "conservative_upper_bound"
        )
        return {
            "budget_nanodollars": self.budget_nanodollars,
            "policy_identifier": self.policy_identifier,
            "reconciled_nanodollars": self._reconciled_nanodollars,
            "conservative_upper_bound_nanodollars": conservative,
            "unresolved_reserved_nanodollars": self._unresolved_nanodollars,
            "available_nanodollars": available,
            "request_count": len(self._reservations),
            "stop_reason": self.stop_reason,
        }
