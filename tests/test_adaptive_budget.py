"""T04: BudgetController counters, sticky exhaustion, and pre-checks."""

import pytest

from gpt_researcher.adaptive.budget import BudgetController, Usage
from gpt_researcher.adaptive.state import BudgetState, StopReason


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_record_accumulates_usage():
    controller = BudgetController(BudgetState())
    controller.record(Usage(input_tokens=100, output_tokens=50, cost_usd=0.01))
    controller.record(Usage(input_tokens=10, output_tokens=5, cost_usd=0.02))
    assert controller.state.llm_calls == 2
    assert controller.state.input_tokens == 110
    assert controller.state.output_tokens == 55
    assert controller.state.cost_usd == pytest.approx(0.03)


def test_rejects_negative_usage():
    controller = BudgetController(BudgetState())
    with pytest.raises(ValueError):
        controller.record(Usage(input_tokens=-1))
    with pytest.raises(ValueError):
        controller.record(Usage(cost_usd=-0.5))


def test_max_rounds_exhaustion_is_sticky():
    controller = BudgetController(BudgetState(max_rounds=1))
    ok, reason = controller.begin_round()
    assert ok and reason is None
    assert controller.state.rounds == 1
    ok, reason = controller.begin_round()
    assert not ok and reason is StopReason.MAX_ROUNDS


def test_first_exhaustion_reason_is_not_overwritten():
    controller = BudgetController(BudgetState(max_llm_calls=1, max_cost_usd=0.001))
    controller.record(Usage(input_tokens=1, output_tokens=1, cost_usd=0.01))
    assert controller.exhaust_reason() is not None
    first = controller.state.first_exhausted
    controller.record(Usage(input_tokens=1, output_tokens=1, cost_usd=0.01))
    assert controller.state.first_exhausted == first


def test_token_limit_uses_input_plus_output():
    controller = BudgetController(BudgetState(max_tokens=100))
    controller.record(Usage(input_tokens=60, output_tokens=40))
    assert controller.exhaust_reason() is StopReason.MAX_TOKENS


def test_cost_limit_triggers():
    controller = BudgetController(BudgetState(max_cost_usd=0.02))
    controller.record(Usage(cost_usd=0.019))
    assert controller.exhaust_reason() is None
    controller.record(Usage(cost_usd=0.002))
    assert controller.exhaust_reason() is StopReason.MAX_COST


def test_time_limit_uses_injected_clock():
    clock = FakeClock()
    controller = BudgetController(BudgetState(max_seconds=10), clock=clock)
    assert controller.exhaust_reason() is None
    clock.advance(11)
    assert controller.exhaust_reason() is StopReason.MAX_SECONDS


def test_can_call_llm_pre_checks():
    controller = BudgetController(BudgetState(max_llm_calls=2, max_tokens=1000))
    assert controller.can_call_llm(est_tokens=100)[0] is True
    controller.record(Usage(input_tokens=10, output_tokens=10))
    controller.record(Usage(input_tokens=10, output_tokens=10))
    ok, reason = controller.can_call_llm(est_tokens=100)
    assert not ok and reason is StopReason.MAX_LLM_CALLS


def test_remaining_never_negative():
    controller = BudgetController(BudgetState(max_rounds=1, max_llm_calls=1))
    controller.begin_round()
    controller.record(Usage(input_tokens=10, output_tokens=10))
    controller.begin_round()
    remaining = controller.remaining()
    assert remaining["rounds"] == 0
    assert remaining["llm_calls"] == 0
