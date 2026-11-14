"""T02: ResearchState round-trip, invariants, and trimming."""

import json

import pytest

from gpt_researcher.adaptive.state import (
    MAX_EVIDENCE,
    BudgetState,
    Claim,
    EvaluationResult,
    Evidence,
    MetricResult,
    PlanSnapshot,
    ResearchState,
    RoundRecord,
    RunStatus,
    StopReason,
    SubTask,
    SupportLevel,
    TaskStatus,
)


def _state(**overrides) -> ResearchState:
    base = dict(run_id="run_1", query="what is rag?", started_at="2026-11-14T00:00:00Z")
    base.update(overrides)
    return ResearchState(**base)


def _task(task_id: str = "T1", **kw) -> SubTask:
    return SubTask(id=task_id, query="q", goal="g", **kw)


def _evidence(ev_id: str = "E1", task_id: str = "T1") -> Evidence:
    return Evidence(
        id=ev_id,
        task_id=task_id,
        url="https://example.com/a",
        title="A",
        snippet="s",
        query="q",
        round=1,
        fetched_at="2026-11-14T00:00:00Z",
    )


def test_round_trip_preserves_everything():
    ev = EvaluationResult(
        round=1,
        mode="light",
        metrics={"coverage": MetricResult(name="coverage", value=0.5)},
        claims=[Claim(id="C1", text="t", section="## s", evidence_ids=["E1"])],
        passed=False,
    )
    state = _state(
        plan=[_task()],
        evidence=[_evidence()],
        claims=[Claim(id="C1", text="t", section="## s", evidence_ids=["E1"])],
        visited_urls={"https://example.com/a"},
        rounds=[RoundRecord(round=1, evaluation=ev)],
        latest_evaluation=ev,
        budget=BudgetState(rounds=1),
        report="# report",
        report_versions=[],
    )
    restored = ResearchState.from_dict(json.loads(state.to_json()))
    assert restored.to_dict() == state.to_dict()
    assert restored.visited_urls == {"https://example.com/a"}


def test_duplicate_task_ids_rejected():
    state = _state(plan=[_task("T1"), _task("T1")])
    with pytest.raises(ValueError, match="duplicate task id"):
        state.validate()


def test_duplicate_evidence_ids_rejected():
    state = _state(plan=[_task()], evidence=[_evidence("E1"), _evidence("E1")])
    with pytest.raises(ValueError, match="duplicate evidence id"):
        state.validate()


def test_evidence_must_reference_known_task():
    state = _state(plan=[_task("T1")], evidence=[_evidence("E1", task_id="T9")])
    with pytest.raises(ValueError, match="unknown task"):
        state.validate()


def test_claim_must_reference_known_evidence():
    state = _state(
        plan=[_task()],
        evidence=[_evidence("E1")],
        claims=[Claim(id="C1", text="t", section="s", evidence_ids=["E9"])],
    )
    with pytest.raises(ValueError, match="unknown evidence"):
        state.validate()


def test_completed_requires_quality_met_and_passing_evaluation():
    state = _state(
        plan=[_task()],
        status=RunStatus.COMPLETED,
        stop_reason=StopReason.MAX_ROUNDS,
        latest_evaluation=EvaluationResult(round=1, mode="light", passed=True),
    )
    with pytest.raises(ValueError, match="quality_met"):
        state.validate()

    ok = _state(
        plan=[_task()],
        status=RunStatus.COMPLETED,
        stop_reason=StopReason.QUALITY_MET,
        latest_evaluation=EvaluationResult(round=1, mode="light", passed=True),
    )
    ok.validate()


def test_completed_without_passing_evaluation_rejected():
    state = _state(
        plan=[_task()],
        status=RunStatus.COMPLETED,
        stop_reason=StopReason.QUALITY_MET,
        latest_evaluation=EvaluationResult(round=1, mode="light", passed=False),
    )
    with pytest.raises(ValueError, match="passing evaluation"):
        state.validate()


def test_partial_requires_open_questions_or_stop_reason():
    state = _state(plan=[_task()], status=RunStatus.PARTIAL)
    with pytest.raises(ValueError, match="open_questions or stop_reason"):
        state.validate()

    state.stop_reason = StopReason.MAX_COST
    state.validate()


def test_budget_rounds_must_match_round_records():
    state = _state(plan=[_task()], budget=BudgetState(rounds=2), rounds=[RoundRecord(round=1)])
    with pytest.raises(ValueError, match="budget.rounds"):
        state.validate()


def test_plan_version_tracks_history():
    state = _state(plan=[_task()], plan_version=1)
    state.validate()
    state.plan_history.append(PlanSnapshot(version=1, round=1))
    with pytest.raises(ValueError, match="plan_version"):
        state.validate()
    state.plan_version = 2
    state.validate()


def test_enforce_limits_truncates_evidence_and_snippets():
    long_snippet = " ".join(["word"] * 500)
    evidence = [
        Evidence(
            id=f"E{i}",
            task_id="T1",
            url=f"https://example.com/{i}",
            title="t",
            snippet=long_snippet,
            query="q",
            round=1,
            fetched_at="now",
        )
        for i in range(MAX_EVIDENCE + 10)
    ]
    state = _state(plan=[_task()], evidence=evidence)
    state.enforce_limits()
    assert len(state.evidence) == MAX_EVIDENCE
    assert len(state.evidence[0].snippet.split()) == 400


def test_open_tasks_and_lookups():
    state = _state(
        plan=[
            _task("T1"),
            _task("T2", status=TaskStatus.DONE),
            _task("T3", status=TaskStatus.FAILED),
        ],
        evidence=[_evidence("E1", "T1")],
    )
    assert [t.id for t in state.open_tasks()] == ["T1"]
    assert state.task_by_id("T2").status is TaskStatus.DONE
    assert state.evidence_by_ids(["E1"])[0].id == "E1"
    assert state.evidence_urls() == {"https://example.com/a"}


def test_support_level_defaults_to_unchecked():
    claim = Claim(id="C1", text="t", section="s")
    assert claim.support is SupportLevel.UNCHECKED
