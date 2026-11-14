"""T21: final report appendix rendering."""

from gpt_researcher.adaptive.report import build_final_report
from gpt_researcher.adaptive.state import (
    Deficit,
    EvaluationResult,
    MetricResult,
    ResearchState,
    RunStatus,
    StopReason,
    SubTask,
    TaskStatus,
)

BODY = """# Report

## Introduction

Intro text. https://e.com/1

## Conclusion

Conclusion text. https://e.com/1

## References

- https://e.com/1
"""


def _evaluation(passed: bool) -> EvaluationResult:
    return EvaluationResult(
        round=1,
        mode="light",
        metrics={
            "coverage": MetricResult(name="coverage", value=0.5, numerator=1, denominator=2),
            "completeness": MetricResult(name="completeness", value=1.0, numerator=4, denominator=4),
        },
        passed=passed,
        deficits=[Deficit(kind="missing_coverage", description="T2 missing", task_id="T2")],
    )


def _state() -> ResearchState:
    return ResearchState(
        run_id="r",
        query="q",
        plan=[
            SubTask(id="T1", query="q1", goal="g1", status=TaskStatus.DONE),
            SubTask(id="T2", query="q2", goal="g2", status=TaskStatus.PENDING),
        ],
    )


def test_completed_run_appends_passing_summary():
    report = build_final_report(
        _state(), BODY, _evaluation(True), RunStatus.COMPLETED, StopReason.QUALITY_MET
    )
    assert "研究质量自检" in report
    assert "已通过全部质量门槛" in report
    assert "研究问题覆盖度" in report
    assert report.index("## References") > report.index("研究质量自检")


def test_partial_run_lists_every_required_section():
    report = build_final_report(
        _state(), BODY, _evaluation(False), RunStatus.PARTIAL, StopReason.MAX_ROUNDS
    )
    for section in (
        "**运行状态**",
        "### 1. 停止原因与资源使用",
        "### 2. 未达标的评估指标",
        "### 3. 未解决的研究任务",
        "### 4. 报告已知薄弱点",
        "### 5. 建议的下一步",
    ):
        assert section in report
    assert "`max_rounds`" in report
    assert "T2" in report


def test_appendix_is_appended_when_no_references_section():
    body = "# Report\n\n## Introduction\n\nText.\n"
    report = build_final_report(
        _state(), body, _evaluation(False), RunStatus.PARTIAL, StopReason.MAX_COST
    )
    assert report.rstrip().endswith("建议的下一步") or "建议的下一步" in report
    assert "研究质量自检" in report


def test_failed_run_still_renders_appendix():
    report = build_final_report(
        _state(), "", _evaluation(False), RunStatus.FAILED, StopReason.PLANNER_EXHAUSTED
    )
    assert "未完成" in report
    assert "planner_exhausted" in report
