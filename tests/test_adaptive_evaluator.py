"""T12/T13: evaluator (light + full) and quality gate."""

import json

import pytest

from gpt_researcher.adaptive.evaluator import QualityGate, ResearchEvaluator
from gpt_researcher.adaptive.state import (
    Claim,
    EvaluationResult,
    Evidence,
    MetricResult,
    ResearchState,
    SubTask,
    SupportLevel,
    TaskStatus,
)

REPORT = """# RAG investigation

## Introduction

This report investigates how retrieval quality shapes the reliability of retrieval-augmented generation systems. https://a.com/x

## RAG limitations

Retrieval failures dominate the error budget of production retrieval-augmented generation deployments, because no generator can recover a document that was never retrieved. https://a.com/x

## RAG retrieval failures

Hallucination still occurs when the retrieved context is irrelevant to the user question, and reranking only partially compensates for weak recall. https://b.com/y

## Conclusion

Retrieval and generation must be evaluated jointly, because each hides the other's failures. https://a.com/x

## References

- https://a.com/x
- https://b.com/y
"""


def make_state(extra_pending: bool = False) -> ResearchState:
    plan = [
        SubTask(id="T1", query="RAG limitations", goal="list RAG limitations", status=TaskStatus.DONE),
        SubTask(
            id="T2",
            query="RAG retrieval failures",
            goal="explain retrieval failures",
            status=TaskStatus.DONE,
        ),
    ]
    if extra_pending:
        plan.append(
            SubTask(id="T3", query="RAG cost analysis", goal="analyze costs", status=TaskStatus.PENDING)
        )
    return ResearchState(
        run_id="run",
        query="how does RAG fail?",
        plan=plan,
        evidence=[
            Evidence(
                id="E1",
                task_id="T1",
                url="https://a.com/x",
                title="A",
                snippet="retrieval failures dominate",
                query="q1",
                round=1,
                fetched_at="2026-11-14T00:00:00Z",
                published_at="2025-06-01",
            ),
            Evidence(
                id="E2",
                task_id="T2",
                url="https://b.com/y",
                title="B",
                snippet="irrelevant context causes hallucination",
                query="q2",
                round=1,
                fetched_at="2026-11-14T00:00:00Z",
                published_at="2025-07-01",
            ),
        ],
    )


async def ok_sampler(url: str) -> str:
    return "ok"


async def test_light_evaluation_passes_for_complete_report():
    evaluator = ResearchEvaluator(sampler=ok_sampler)
    result = await evaluator.evaluate(make_state(), REPORT, mode="light")

    assert result.mode == "light"
    assert result.llm_calls == 0
    assert result.value("coverage") == 1.0
    assert result.value("citation_match_url") == 1.0
    assert result.value("completeness") == 1.0
    assert result.passed is True
    assert result.deficits == []


async def test_light_evaluation_generates_coverage_deficit():
    evaluator = ResearchEvaluator(sampler=ok_sampler)
    result = await evaluator.evaluate(make_state(extra_pending=True), REPORT, mode="light")

    assert result.value("coverage") == pytest.approx(2 / 3)
    assert result.passed is False
    kinds = [d.kind for d in result.deficits]
    assert "missing_coverage" in kinds
    deficit = next(d for d in result.deficits if d.kind == "missing_coverage")
    assert deficit.task_id == "T3"
    assert deficit.suggested_query == "RAG cost analysis"


async def test_light_evaluation_flags_broken_citations():
    report = REPORT.replace("https://a.com/x", "https://not-collected.com/z").replace(
        "https://b.com/y", "https://not-collected.com/w"
    )
    evaluator = ResearchEvaluator(sampler=ok_sampler)
    result = await evaluator.evaluate(make_state(), report, mode="light")

    assert result.value("citation_match_url") == 0.0
    assert any(d.kind == "broken_citation" for d in result.deficits)


async def test_light_evaluation_reports_structure_deficit():
    report = "## RAG limitations\n\n" + "words " * 40 + " https://a.com/x\n"
    evaluator = ResearchEvaluator(sampler=ok_sampler)
    result = await evaluator.evaluate(make_state(), report, mode="light")
    assert any(d.kind == "incomplete_section" for d in result.deficits)


async def test_full_mode_without_judge_degrades_to_light():
    evaluator = ResearchEvaluator(sampler=ok_sampler, judge=None)
    result = await evaluator.evaluate(make_state(), REPORT, mode="full")
    assert result.mode == "light"
    assert result.llm_calls == 0


async def test_full_mode_judges_support_and_conflicts():
    prompts = []

    async def judge(prompt: str) -> str:
        prompts.append(prompt)
        if "audit whether" in prompt:
            return json.dumps(
                [
                    {"claim_id": "C1", "verdict": "supported", "evidence_id": "E1", "reason": "states it"},
                    {"claim_id": "C2", "verdict": "unsupported", "evidence_id": "", "reason": "no source"},
                ]
            )
        return json.dumps({"conflicts": []})

    evaluator = ResearchEvaluator(sampler=ok_sampler, judge=judge)
    result = await evaluator.evaluate(make_state(), REPORT, mode="full")

    assert result.mode == "full"
    assert result.llm_calls >= 2
    assert "support_rate" in result.metrics
    assert result.value("support_rate") == pytest.approx(0.5)
    assert result.value("conflict_rate") == 0.0
    supported = [c for c in result.claims if c.support is SupportLevel.SUPPORTED]
    unsupported = [c for c in result.claims if c.support is SupportLevel.UNSUPPORTED]
    assert supported and unsupported
    assert any(d.kind == "weak_evidence" for d in result.deficits)
    assert result.passed is False


async def test_judge_failure_does_not_crash_evaluation():
    async def broken_judge(prompt: str) -> str:
        raise RuntimeError("model down")

    evaluator = ResearchEvaluator(sampler=ok_sampler, judge=broken_judge)
    result = await evaluator.evaluate(make_state(), REPORT, mode="full")
    assert result.mode == "full"
    assert result.value("support_rate") is None
    assert result.passed is False


async def test_judge_hallucinated_evidence_id_is_downgraded():
    async def judge(prompt: str) -> str:
        if "audit whether" in prompt:
            return json.dumps(
                [
                    {"claim_id": "C1", "verdict": "supported", "evidence_id": "E999", "reason": "made up"},
                ]
            )
        return json.dumps({"conflicts": []})

    evaluator = ResearchEvaluator(sampler=ok_sampler, judge=judge)
    result = await evaluator.evaluate(make_state(), REPORT, mode="full")
    assert result.value("support_rate") == 0.0
    assert all(c.support is not SupportLevel.SUPPORTED for c in result.claims)


def test_gate_requires_all_core_metrics():
    gate = QualityGate()
    evaluation = EvaluationResult(
        round=1,
        mode="light",
        metrics={
            "coverage": MetricResult(name="coverage", value=1.0),
            "citation_match_url": MetricResult(name="citation_match_url", value=None),
            "completeness": MetricResult(name="completeness", value=1.0),
        },
    )
    assert gate.check(evaluation, "light") is False


def test_gate_full_mode_requires_support_rate():
    gate = QualityGate()
    evaluation = EvaluationResult(
        round=1,
        mode="full",
        metrics={
            "coverage": MetricResult(name="coverage", value=1.0),
            "citation_match_url": MetricResult(name="citation_match_url", value=1.0),
            "completeness": MetricResult(name="completeness", value=1.0),
            "support_rate": MetricResult(name="support_rate", value=0.5),
            "conflict_rate": MetricResult(name="conflict_rate", value=0.0),
        },
    )
    assert gate.check(evaluation, "light") is True
    assert gate.check(evaluation, "full") is False
