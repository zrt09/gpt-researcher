"""Budget accounting and termination checks.

The controller owns the counters that live in ``BudgetState`` so the state file
and the runtime view can never disagree. ``Usage`` is reported by
``utils.llm.create_chat_completion`` through its optional ``usage_sink`` hook,
which makes this the single place where token/call accounting happens.

Design rules (docs/改造方案/09_动态决策与终止机制.md):

* The first exhausted limit is sticky - later checks never overwrite it.
* Pre-checks are cheap estimates; the reported usage is authoritative.
* Nothing here calls the network or an LLM.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .state import BudgetState, StopReason


@dataclass(frozen=True)
class Usage:
    """One LLM call's resource consumption."""

    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    step: str = "adaptive"
    estimated: bool = False


class BudgetController:
    """Counts resource usage and decides whether the loop may continue."""

    def __init__(
        self,
        state: BudgetState,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state = state
        self._clock = clock
        self._started = clock()

    # ------------------------------------------------------------------ clock
    def _sync_elapsed(self) -> None:
        self.state.elapsed_s = max(0.0, self._clock() - self._started)

    # --------------------------------------------------------------- counters
    def record(self, usage: Usage) -> None:
        """Apply one LLM call's usage. Raises on nonsensical values."""
        for name in ("input_tokens", "output_tokens"):
            value = getattr(usage, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"usage.{name} must be a non-negative int, got {value!r}")
        if usage.cost_usd < 0:
            raise ValueError(f"usage.cost_usd must be >= 0, got {usage.cost_usd!r}")

        self.state.llm_calls += 1
        self.state.input_tokens += usage.input_tokens
        self.state.output_tokens += usage.output_tokens
        self.state.cost_usd += usage.cost_usd
        self._sync_elapsed()
        self._mark_if_exceeded()

    def _mark_if_exceeded(self) -> None:
        if self.state.first_exhausted is not None:
            return
        reason = self._evaluate_limits(ignore_calls=False)
        if reason is not None:
            self.state.first_exhausted = reason.value

    # ----------------------------------------------------------------- checks
    def _evaluate_limits(self, ignore_calls: bool = False) -> StopReason | None:
        s = self.state
        if s.rounds >= s.max_rounds:
            return StopReason.MAX_ROUNDS
        if not ignore_calls and s.llm_calls >= s.max_llm_calls:
            return StopReason.MAX_LLM_CALLS
        if s.input_tokens + s.output_tokens >= s.max_tokens:
            return StopReason.MAX_TOKENS
        if s.cost_usd >= s.max_cost_usd:
            return StopReason.MAX_COST
        if s.elapsed_s >= s.max_seconds:
            return StopReason.MAX_SECONDS
        return None

    def exhaust_reason(self) -> StopReason | None:
        """Sticky first exhaustion reason, or None when the loop may continue."""
        self._sync_elapsed()
        if self.state.first_exhausted is not None:
            try:
                return StopReason(self.state.first_exhausted)
            except ValueError:
                return StopReason.ERROR
        reason = self._evaluate_limits()
        if reason is not None:
            self.state.first_exhausted = reason.value
        return reason

    def can_start_round(self) -> tuple[bool, StopReason | None]:
        """Pure check: may another round begin?"""
        reason = self.exhaust_reason()
        return reason is None, reason

    def begin_round(self) -> tuple[bool, StopReason | None]:
        """Check and, when allowed, count the round as started."""
        ok, reason = self.can_start_round()
        if ok:
            self.state.rounds += 1
        return ok, reason

    def can_call_llm(self, est_tokens: int = 1500) -> tuple[bool, StopReason | None]:
        """Conservative pre-check before spending a call."""
        self._sync_elapsed()
        s = self.state
        if s.llm_calls + 1 > s.max_llm_calls:
            return False, StopReason.MAX_LLM_CALLS
        if s.input_tokens + s.output_tokens + est_tokens > s.max_tokens:
            return False, StopReason.MAX_TOKENS
        if s.cost_usd >= s.max_cost_usd:
            return False, StopReason.MAX_COST
        if s.elapsed_s >= s.max_seconds:
            return False, StopReason.MAX_SECONDS
        return True, None

    # ----------------------------------------------------------------- report
    def remaining(self) -> dict[str, float | int]:
        self._sync_elapsed()
        s = self.state
        return {
            "rounds": max(0, s.max_rounds - s.rounds),
            "llm_calls": max(0, s.max_llm_calls - s.llm_calls),
            "tokens": max(0, s.max_tokens - s.input_tokens - s.output_tokens),
            "seconds": max(0.0, s.max_seconds - s.elapsed_s),
            "cost_usd": round(max(0.0, s.max_cost_usd - s.cost_usd), 6),
        }
