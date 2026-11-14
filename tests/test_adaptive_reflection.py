"""T16: reflection decision table (rule-driven, offline)."""

import pytest

from gpt_researcher.adaptive.budget import BudgetController
from gpt_researcher.adaptive.reflection import Action, ReflectionEngine, has_no_progress
from gpt_researcher.adaptive.state import (
    BudgetState,
    Deficit,
    EvaluationResult,
    MetricResult,
    ResearchState,
    RoundRecord,
    StopReason,
    SubTask,
    TaskOrigin,
    TaskStatus,
)


def metric(name, value):
    return MetricResult(name=name, value=value)


def evaluation(**values) -> EvaluationResult:
    metrics = {
        name: metric(name, value)
        for name, value in values.items()
        if name in ("coverage", "citation_match_url", "completeness", "support_rate", "conflict_rate")
    }
    return EvaluationResult(round=1, mode="light", metrics=metrics, passed=False)


def state(plan=None, rounds=None) -> ResearchState:
    return ResearchState(
        run_id="r",
        query="q",
        plan=plan if plan is not None else [SubTask(id="T1", query="q", goal="g")],
        rounds=rounds or [],
    )


def budget(**kwargs) -> BudgetController:
    return BudgetController(BudgetState(**kwargs))


def test_budget_exhaustion_wins_over_everything():
    decision = ReflectionEngine().decide(
        evaluation(coverage=0.1, citation_match_url=0.1, completeness=0.1),
        state(),
        budget(rounds=1, max_rounds=1),
    )
    assert decision.action is Action.FINISH
    assert decision.stop_reason is StopReason.MAX_ROUNDS


def test_passing_evaluation_finishes_with_quality_met():
    ev = evaluation(coverage=0.9, citation_match_url=0.9, completeness=1.0)
    ev.passed = True
    decision = ReflectionEngine().decide(ev, state(), budget())
    assert decision.action is Action.FINISH
    assert decision.stop_reason is StopReason.QUALITY_MET


def test_missing_core_metrics_aborts():
    decision = ReflectionEngine().decide(evaluation(), state(), budget())
    assert decision.action is Action.ABORT


def test_no_progress_finishes():
    rounds = [
        RoundRecord(round=1, new_evidence=0, evaluation=evaluation(coverage=0.5, citation_match_url=0.5, completeness=0.5)),
        RoundRecord(round=2, new_evidence=0, evaluation=evaluation(coverage=0.5, citation_match_url=0.5, completeness=0.5)),
    ]
    assert has_no_progress(state(rounds=rounds), window=2) is True
    decision = ReflectionEngine().decide(
        evaluation(coverage=0.5, citation_match_url=0.5, completeness=0.5),
        state(rounds=rounds),
        budget(),
    )
    assert decision.action is Action.FINISH
    assert decision.stop_reason is StopReason.NO_PROGRESS


def test_improvement_resets_no_progress():
    rounds = [
        RoundRecord(round=1, new_evidence=1, evaluation=evaluation(coverage=0.4, citation_match_url=0.5, completeness=0.5)),
        RoundRecord(round=2, new_evidence=1, evaluation=evaluation(coverage=0.6, citation_match_url=0.5, completeness=0.5)),
    ]
    assert has_no_progress(state(rounds=rounds), window=2) is False


def test_single_missing_coverage_supplements():
    ev = evaluation(coverage=0.5, citation_match_url=1.0, completeness=1.0)
    ev.deficits = [Deficit(kind="missing_coverage", description="T2 missing", task_id="T2")]
    decision = ReflectionEngine().decide(ev, state(), budget())
    assert decision.action is Action.SUPPLEMENT
    assert decision.addressed[0].task_id == "T2"


def test_weak_evidence_with_poor_citations_supplements():
    ev = evaluation(coverage=1.0, citation_match_url=0.2, completeness=1.0)
    ev.deficits = [Deficit(kind="weak_evidence", description="claim weak", claim_id="C1")]
    decision = ReflectionEngine().decide(ev, state(), budget())
    assert decision.action is Action.SUPPLEMENT


def test_weak_evidence_with_strong_citations_rewrites():
    ev = evaluation(coverage=1.0, citation_match_url=0.95, completeness=1.0)
    ev.deficits = [Deficit(kind="weak_evidence", description="claim overstates", claim_id="C1")]
    decision = ReflectionEngine().decide(ev, state(), budget())
    assert decision.action is Action.REWRITE
    assert decision.rewrite_notes


def test_broken_citations_supplement():
    ev = evaluation(coverage=1.0, citation_match_url=0.3, completeness=1.0)
    ev.deficits = [Deficit(kind="broken_citation", description="url gone")]
    decision = ReflectionEngine().decide(ev, state(), budget())
    assert decision.action is Action.SUPPLEMENT


def test_conflicts_supplement():
    ev = evaluation(coverage=1.0, citation_match_url=1.0, completeness=1.0)
    ev.deficits = [Deficit(kind="conflict", description="two numbers disagree")]
    decision = ReflectionEngine().decide(ev, state(), budget())
    assert decision.action is Action.SUPPLEMENT


def test_structure_deficit_rewrites():
    ev = evaluation(coverage=1.0, citation_match_url=1.0, completeness=0.5)
    ev.deficits = [Deficit(kind="incomplete_section", description="missing conclusion")]
    decision = ReflectionEngine().decide(ev, state(), budget())
    assert decision.action is Action.REWRITE


def test_two_failed_initial_tasks_replan():
    plan = [
        SubTask(id="T1", query="q1", goal="g1", origin=TaskOrigin.INITIAL, attempts=1, status=TaskStatus.FAILED),
        SubTask(id="T2", query="q2", goal="g2", origin=TaskOrigin.INITIAL, attempts=1, status=TaskStatus.DONE),
    ]
    ev = evaluation(coverage=0.3, citation_match_url=1.0, completeness=1.0)
    ev.deficits = [
        Deficit(kind="missing_coverage", description="T1 missing", task_id="T1"),
        Deficit(kind="missing_coverage", description="T2 missing", task_id="T2"),
    ]
    decision = ReflectionEngine().decide(ev, state(plan=plan), budget())
    assert decision.action is Action.REPLAN


def test_no_deficits_falls_back_to_finish():
    ev = evaluation(coverage=0.7, citation_match_url=0.9, completeness=0.9)
    decision = ReflectionEngine().decide(ev, state(), budget())
    assert decision.action is Action.FINISH
    assert decision.stop_reason is StopReason.NO_PROGRESS
