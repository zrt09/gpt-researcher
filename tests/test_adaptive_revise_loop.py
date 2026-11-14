"""T17/T18: gap-driven revision and a two-round loop without network access."""

import json

import pytest

from gpt_researcher.adaptive.budget import BudgetController
from gpt_researcher.adaptive.evaluator import ResearchEvaluator
from gpt_researcher.adaptive.executor import ResearchExecutor
from gpt_researcher.adaptive.planner import Planner
from gpt_researcher.adaptive.reflection import Action, ReflectionEngine
from gpt_researcher.adaptive.state import (
    Deficit,
    EvaluationResult,
    Evidence,
    ResearchState,
    SubTask,
    TaskOrigin,
    TaskStatus,
)

# --------------------------------------------------------------------- T17


def _evaluation(deficits) -> EvaluationResult:
    ev = EvaluationResult(round=1, mode="light")
    ev.deficits = list(deficits)
    return ev


def _state(plan) -> ResearchState:
    return ResearchState(run_id="r", query="q", plan=list(plan))


async def test_revise_uses_suggested_query_without_calling_llm():
    async def forbidden(prompt: str) -> str:  # pragma: no cover - must not run
        raise AssertionError("LLM must not be called when a query is suggested")

    planner = Planner(llm_call=forbidden)
    state = _state(
        [SubTask(id="T1", query="q1", goal="g1", status=TaskStatus.DONE, attempts=1)]
    )
    evaluation = _evaluation(
        [
            Deficit(
                kind="missing_coverage",
                description="T1 not covered",
                task_id="T1",
                suggested_query="a sharper query",
            )
        ]
    )

    tasks = await planner.revise(state, evaluation)

    assert len(tasks) == 1
    assert tasks[0].query == "a sharper query"
    assert tasks[0].origin is TaskOrigin.REPLAN
    assert tasks[0].parent_id == "T1"
    assert tasks[0].id == "T2"


async def test_revise_asks_llm_when_no_query_suggested():
    calls = []

    async def fake_llm(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(["generated gap query"])

    planner = Planner(llm_call=fake_llm)
    state = _state(
        [SubTask(id="T1", query="q1", goal="g1", status=TaskStatus.DONE, attempts=1)]
    )
    evaluation = _evaluation(
        [Deficit(kind="weak_evidence", description="claim lacks support", claim_id="C1")]
    )

    tasks = await planner.revise(state, evaluation)

    assert len(calls) == 1
    assert tasks[0].query == "generated gap query"
    assert tasks[0].origin is TaskOrigin.GAP


async def test_revise_skips_tasks_that_are_already_queued():
    planner = Planner()
    state = _state([SubTask(id="T1", query="q1", goal="g1", status=TaskStatus.PENDING)])
    evaluation = _evaluation(
        [
            Deficit(
                kind="missing_coverage",
                description="T1 missing",
                task_id="T1",
                suggested_query="q1",
            )
        ]
    )
    assert await planner.revise(state, evaluation) == []


async def test_revise_returns_nothing_without_gap_deficits():
    planner = Planner()
    state = _state([])
    evaluation = _evaluation([Deficit(kind="incomplete_section", description="no conclusion")])
    assert await planner.revise(state, evaluation) == []


async def test_revise_respects_the_task_budget():
    planner = Planner(max_tasks=2)
    state = _state(
        [
            SubTask(id="T1", query="q1", goal="g1", status=TaskStatus.DONE, attempts=1),
            SubTask(id="T2", query="q2", goal="g2", status=TaskStatus.DONE, attempts=1),
        ]
    )
    evaluation = _evaluation(
        [
            Deficit(kind="broken_citation", description="u1", suggested_query="x1"),
            Deficit(kind="broken_citation", description="u2", suggested_query="x2"),
        ]
    )
    assert await planner.revise(state, evaluation) == []  # no room left


# --------------------------------------------------------------------- T18


def _report(state: ResearchState, covered_tasks: set[str]) -> str:
    """Render a full report whose sections cite the evidence of covered tasks."""
    urls = [f"https://e.com/{t.id.lower()}" for t in state.plan if t.id in covered_tasks]
    main_url = urls[0] if urls else "https://e.com/none"
    lines = [
        "# Report",
        "",
        "## Introduction",
        "",
        "This report studies the question under test " * 3 + main_url,
        "",
    ]
    for task in state.plan:
        if task.id not in covered_tasks:
            continue
        task_url = f"https://e.com/{task.id.lower()}"
        body = (
            f"The analysis for {task.goal} finds that retrieval quality dominates outcomes "
            f"and that evaluation must stay in the loop for every deployment. {task_url}"
        )
        lines += [f"## {task.query}", "", body, ""]
    lines += [
        "## Conclusion",
        "",
        "Retrieval and generation must be evaluated together because each hides the other's failures. "
        + main_url,
        "",
        "## References",
        "",
        *[f"- {url}" for url in urls],
    ]
    return "\n".join(lines)


class FakeExecutor(ResearchExecutor):
    """Executor that fabricates one evidence record per task."""

    def __init__(self):
        super().__init__(researcher_factory=lambda **kw: None)
        self.runs = 0

    async def run_task(self, task, *, round_no=1, agent=None, role=None):
        from gpt_researcher.adaptive.executor import TaskResult

        self.runs += 1
        evidence = Evidence(
            id=f"E{self.runs}",
            task_id=task.id,
            url=f"https://e.com/{task.id.lower()}",
            title=task.id,
            snippet="retrieval quality dominates outcomes",
            query=task.query,
            round=round_no,
            fetched_at="2026-11-14T00:00:00Z",
            published_at="2025-06-01",
        )
        return TaskResult(
            task_id=task.id,
            status=TaskStatus.DONE,
            evidence=[evidence],
            context="context",
            tokens=10,
            cost_usd=0.001,
        )


async def ok_sampler(url: str) -> str:
    return "ok"


async def test_two_round_loop_moves_from_under_covered_to_quality_met():
    async def plan_llm(prompt: str) -> str:
        return json.dumps(
            [
                {"query": "RAG limitations", "goal": "list RAG limitations"},
                {"query": "RAG retrieval failures", "goal": "explain retrieval failures"},
            ]
        )

    planner = Planner(llm_call=plan_llm)
    state = ResearchState(run_id="r", query="how does RAG fail?")
    state.plan = await planner.initial_plan(state.query)
    budget = BudgetController(state.budget)
    executor = FakeExecutor()
    evaluator = ResearchEvaluator(sampler=ok_sampler)
    reflector = ReflectionEngine()

    # --- round 1: only the first task is executed this round
    first = state.plan[0]
    result = await executor.run_task(first, round_no=1)
    state.evidence.extend(result.evidence)
    first.status = TaskStatus.DONE
    first.attempts = 1
    first.evidence_ids = [e.id for e in result.evidence]

    report = _report(state, covered_tasks={"T1"})
    evaluation = await evaluator.evaluate(state, report, mode="light")
    state.rounds.append(_record(state, evaluation, new_evidence=len(result.evidence)))
    budget.begin_round()

    assert evaluation.passed is False
    assert evaluation.value("coverage") == pytest.approx(0.5)

    decision = reflector.decide(evaluation, state, budget)
    assert decision.action is Action.SUPPLEMENT
    new_tasks = await planner.revise(state, evaluation)
    assert new_tasks == []  # the pending task will simply run next round

    # --- round 2: execute the remaining plan task
    second = state.plan[1]
    result = await executor.run_task(second, round_no=2)
    state.evidence.extend(result.evidence)
    second.status = TaskStatus.DONE
    second.attempts = 1
    second.evidence_ids = [e.id for e in result.evidence]

    report = _report(state, covered_tasks={"T1", "T2"})
    evaluation = await evaluator.evaluate(state, report, mode="light")
    state.rounds.append(_record(state, evaluation, new_evidence=len(result.evidence)))
    budget.begin_round()

    assert evaluation.value("coverage") == pytest.approx(1.0)
    assert evaluation.passed is True

    decision = reflector.decide(evaluation, state, budget)
    assert decision.action is Action.FINISH
    assert decision.stop_reason.value == "quality_met"


def _record(state: ResearchState, evaluation, new_evidence: int):
    from gpt_researcher.adaptive.state import RoundRecord

    return RoundRecord(
        round=len(state.rounds) + 1,
        plan_version=state.plan_version,
        new_evidence=new_evidence,
        evaluation=evaluation,
    )
