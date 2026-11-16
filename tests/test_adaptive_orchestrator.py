"""T19-T21: orchestrator loop, terminal states, and the self-check appendix."""

import json

import pytest

from gpt_researcher.adaptive.executor import TaskResult
from gpt_researcher.adaptive.orchestrator import AdaptiveResearcher
from gpt_researcher.adaptive.state import (
    Deficit,
    EvaluationResult,
    Evidence,
    MetricResult,
    ResearchState,
    RunStatus,
    StopReason,
    SubTask,
    TaskStatus,
)

REPORT = """# Report

## Introduction

This report studies the question with citations. https://e.com/1

## Findings

Retrieval quality dominates the outcome of the pipeline in production. https://e.com/1

## Conclusion

Retrieval and generation must be evaluated together. https://e.com/1

## References

- https://e.com/1
"""


def make_evaluation(passed: bool, coverage=0.5, deficits=None) -> EvaluationResult:
    metrics = {
        "coverage": MetricResult(name="coverage", value=coverage, numerator=1, denominator=2),
        "citation_match_url": MetricResult(name="citation_match_url", value=1.0, numerator=1, denominator=1),
        "completeness": MetricResult(name="completeness", value=1.0, numerator=4, denominator=4),
    }
    return EvaluationResult(
        round=1,
        mode="light",
        metrics=metrics,
        passed=passed,
        deficits=list(deficits or []),
    )


class FakePlanner:
    def __init__(self, tasks, revisions=None, fail_initial=False):
        self.tasks = tasks
        self.revisions = list(revisions or [])
        self.fail_initial = fail_initial
        self.revise_calls = 0

    async def initial_plan(self, query):
        if self.fail_initial:
            raise RuntimeError("planner down")
        return list(self.tasks)

    async def revise(self, state, evaluation, max_new_tasks=None):
        self.revise_calls += 1
        return list(self.revisions.pop(0)) if self.revisions else []


class FakeExecutor:
    def __init__(self):
        self.runs = 0
        self.calls = []

    async def run_task(self, task, *, round_no=1, agent=None, role=None):
        self.runs += 1
        self.calls.append({"agent": agent, "role": role})
        return TaskResult(
            task_id=task.id,
            status=TaskStatus.DONE,
            evidence=[
                Evidence(
                    id=f"E{self.runs}",
                    task_id=task.id,
                    url=f"https://e.com/{self.runs}",
                    title=task.id,
                    snippet="snippet",
                    query=task.query,
                    round=round_no,
                    fetched_at="2026-11-14T00:00:00Z",
                )
            ],
            context="context",
            tokens=10,
            cost_usd=0.001,
        )


class FakeEvaluator:
    def __init__(self, evaluations):
        self.evaluations = list(evaluations)
        self.calls = 0

    async def evaluate(self, state, report, mode=None, round_no=None):
        self.calls += 1
        return self.evaluations[min(self.calls - 1, len(self.evaluations) - 1)]


class FakeWriter:
    def __init__(self, text=REPORT):
        self.text = text
        self.notes = []

    async def __call__(self, state, notes):
        self.notes.append(list(notes))
        return self.text


def _tasks(n=2):
    return [
        SubTask(id=f"T{i + 1}", query=f"query {i + 1}", goal=f"goal {i + 1}")
        for i in range(n)
    ]


def build(**kwargs) -> AdaptiveResearcher:
    query = kwargs.pop("query", "q")
    kwargs.setdefault("planner", FakePlanner(_tasks()))
    kwargs.setdefault("executor", FakeExecutor())
    kwargs.setdefault("writer", FakeWriter())
    return AdaptiveResearcher(query, persist=False, **kwargs)


async def test_first_round_quality_met_completes():
    evaluator = FakeEvaluator([make_evaluation(True, coverage=1.0)])
    adaptive = build(evaluator=evaluator, query="2026年诺贝尔物理学奖得主")

    result = await adaptive.run()

    assert result.status is RunStatus.COMPLETED
    assert result.stop_reason is StopReason.QUALITY_MET
    assert result.state.budget.rounds == 1
    assert "研究质量自检" in result.report
    assert "已通过全部质量门槛" in result.report
    # References must stay last.
    assert result.report.index("## References") > result.report.index("研究质量自检")


async def test_appendix_follows_the_report_language():
    evaluator = FakeEvaluator([make_evaluation(True, coverage=1.0)])
    adaptive = build(evaluator=evaluator, query="Who won the 2026 Nobel Prize in Physics?")

    result = await adaptive.run()

    assert "Appendix: Quality Self-Check" in result.report
    assert "研究质量自检" not in result.report


async def test_supplement_adds_tasks_and_next_round_can_pass():
    evaluator = FakeEvaluator(
        [
            make_evaluation(
                False,
                coverage=0.5,
                deficits=[Deficit(kind="broken_citation", description="citation url dead")],
            ),
            make_evaluation(True, coverage=1.0),
        ]
    )
    planner = FakePlanner(_tasks(), revisions=[[SubTask(id="T3", query="q3", goal="g3")]])
    adaptive = build(planner=planner, evaluator=evaluator)

    result = await adaptive.run()

    assert result.status is RunStatus.COMPLETED
    assert result.state.budget.rounds == 2
    assert planner.revise_calls == 1
    assert any(t.id == "T3" for t in result.state.plan)
    assert result.state.plan_version == 2


async def test_budget_exhaustion_yields_partial_with_full_appendix():
    evaluator = FakeEvaluator([make_evaluation(False, coverage=0.3)])
    adaptive = build(evaluator=evaluator, max_rounds=1, query="研究问题：RAG 的局限")

    result = await adaptive.run()

    assert result.status is RunStatus.PARTIAL
    assert result.stop_reason is StopReason.MAX_ROUNDS
    for section in (
        "### 1. 停止原因与资源使用",
        "### 2. 未达标的评估指标",
        "### 3. 未解决的研究任务",
        "### 4. 报告已知薄弱点",
        "### 5. 建议的下一步",
    ):
        assert section in result.report
    assert result.state.open_questions


async def test_planner_failure_is_terminal_failed():
    planner = FakePlanner([], fail_initial=True)
    adaptive = build(planner=planner, evaluator=FakeEvaluator([make_evaluation(False)]))

    result = await adaptive.run()

    assert result.status is RunStatus.FAILED
    assert result.stop_reason is StopReason.PLANNER_EXHAUSTED


async def test_persistence_writes_state_and_report_versions(tmp_path):
    evaluator = FakeEvaluator([make_evaluation(True, coverage=1.0)])
    adaptive = AdaptiveResearcher(
        "q",
        planner=FakePlanner(_tasks()),
        executor=FakeExecutor(),
        writer=FakeWriter(),
        evaluator=evaluator,
        state_dir=str(tmp_path),
        persist=True,
    )

    result = await adaptive.run()

    directory = tmp_path / result.state.run_id
    assert (directory / "state.json").exists()
    assert (directory / "report_v1.md").exists()
    assert (directory / "final_report.md").exists()
    saved = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    assert saved["status"] == "completed"


async def test_quality_met_on_the_last_allowed_round_is_completed():
    evaluator = FakeEvaluator([make_evaluation(True, coverage=1.0)])
    adaptive = build(evaluator=evaluator, max_rounds=1)

    result = await adaptive.run()

    assert result.status is RunStatus.COMPLETED
    assert result.stop_reason is StopReason.QUALITY_MET


async def test_rounds_record_tasks_and_evidence():
    evaluator = FakeEvaluator([make_evaluation(True, coverage=1.0)])
    adaptive = build(evaluator=evaluator)

    result = await adaptive.run()

    record = result.state.rounds[0]
    assert record.tasks_done == ["T1", "T2"]
    assert record.new_evidence == 2
    assert record.evaluation is not None
    assert result.state.budget.rounds == len(result.state.rounds)


async def test_claims_are_copied_into_state():
    evaluator = FakeEvaluator([make_evaluation(True, coverage=1.0)])
    adaptive = build(evaluator=evaluator)

    result = await adaptive.run()

    assert result.state.claims == list(result.evaluation.claims)


async def test_executor_receives_the_shared_role():
    executor = FakeExecutor()
    evaluator = FakeEvaluator([make_evaluation(True, coverage=1.0)])
    adaptive = build(executor=executor, evaluator=evaluator)

    await adaptive.run()

    assert executor.calls
    assert all(call["agent"] == "researcher" for call in executor.calls)
    assert all(call["role"] for call in executor.calls)


class BrokenWriter:
    async def __call__(self, state, notes):
        raise RuntimeError("writer down")


async def test_writer_failure_degrades_to_evidence_digest():
    adaptive = build(
        writer=BrokenWriter(),
        evaluator=FakeEvaluator([make_evaluation(False, coverage=0.0)]),
        max_rounds=1,
    )

    result = await adaptive.run()

    assert result.report.strip()
    assert "自动生成" in result.report
    assert result.status is RunStatus.PARTIAL
    assert any("写作模型不可用" in q for q in result.state.open_questions)


def test_evidence_context_is_bounded():
    from gpt_researcher.adaptive.orchestrator import AdaptiveResearcher

    evidence = [
        Evidence(
            id=f"E{i}",
            task_id="T1",
            url=f"https://e.com/{i}",
            title="t",
            snippet="x" * 2000,
            query="q",
            round=1,
            fetched_at="now",
        )
        for i in range(40)
    ]
    state = ResearchState(
        run_id="r", query="q", plan=[SubTask(id="T1", query="q1", goal="g1")], evidence=evidence
    )

    context = AdaptiveResearcher._evidence_context(state, [], max_items=6, snippet_chars=100)

    assert context.count("\n- ") <= 6
    assert "x" * 200 not in context
