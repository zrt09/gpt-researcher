"""Final report assembly.

The report body comes from the writer; this module only appends the quality
self-check. When the run did not meet the quality gate, the appendix is the
machine-enforced form of "never fake completion": it must list the stop reason,
the failing metrics, the unresolved tasks, the weak points and a next step.
"""

from __future__ import annotations

import re

from .state import (
    EvaluationResult,
    ResearchState,
    RunStatus,
    StopReason,
    SupportLevel,
    TaskStatus,
)

_REFERENCES_RE = re.compile(
    r"(?m)^(?P<hashes>#{1,6})\s*(?:references|参考文献|参考来源|参考书目)\s*$",
    re.IGNORECASE,
)

_METRIC_LABELS = {
    "coverage": "研究问题覆盖度",
    "citation_match_url": "引用可回溯率",
    "support_rate": "关键结论证据支持率",
    "citation_match_semantic": "引用支撑结论率",
    "source_validity": "来源有效性",
    "source_freshness": "来源时效性",
    "conflict_rate": "结论冲突率",
    "completeness": "结果完整性",
}

_STATUS_TEXT = {
    RunStatus.COMPLETED: "已完成（质量门槛全部满足）",
    RunStatus.PARTIAL: "部分完成（存在未解决的问题，见下）",
    RunStatus.FAILED: "未完成（研究过程失败）",
}


def _fmt_metric(value: float | None) -> str:
    return "无法计算" if value is None else f"{value:.2f}"


def _metrics_table(evaluation: EvaluationResult | None) -> list[str]:
    if evaluation is None or not evaluation.metrics:
        return ["| 指标 | 结果 | 说明 |", "| --- | --- | --- |", "| （无评估数据） | - | - |"]
    lines = ["| 指标 | 结果 | 说明 |", "| --- | --- | --- |"]
    for name, metric in evaluation.metrics.items():
        label = _METRIC_LABELS.get(name, name)
        detail = metric.notes or ""
        if metric.denominator:
            detail = f"{detail} ({metric.numerator}/{metric.denominator})".strip()
        lines.append(f"| {label} | {_fmt_metric(metric.value)} | {detail} |")
    return lines


def _insert_before_references(report: str, appendix: str) -> str:
    match = _REFERENCES_RE.search(report or "")
    if not match:
        return (report or "").rstrip() + "\n\n" + appendix + "\n"
    head = report[: match.start()].rstrip()
    tail = report[match.start() :].strip()
    return f"{head}\n\n{appendix}\n\n{tail}\n"


def build_final_report(
    state: ResearchState,
    report: str,
    evaluation: EvaluationResult | None,
    status: RunStatus,
    stop_reason: StopReason | None,
    budget=None,
) -> str:
    """Prepend nothing, append the self-check appendix, keep References last."""
    if status is RunStatus.COMPLETED:
        appendix = _completed_appendix(evaluation)
    else:
        appendix = _incomplete_appendix(state, evaluation, status, stop_reason, budget)
    return _insert_before_references(report, appendix)


def _completed_appendix(evaluation: EvaluationResult | None) -> str:
    lines = ["## 研究质量自检", "", "本次研究已通过全部质量门槛。", "", *_metrics_table(evaluation)]
    return "\n".join(lines)


def _incomplete_appendix(
    state: ResearchState,
    evaluation: EvaluationResult | None,
    status: RunStatus,
    stop_reason: StopReason | None,
    budget,
) -> str:
    lines = [
        "## 研究质量自检",
        "",
        f"**运行状态**：{_STATUS_TEXT.get(status, status.value)}",
        "",
        "### 1. 停止原因与资源使用",
        "",
        f"- 停止原因：`{stop_reason.value if stop_reason else 'unknown'}`",
    ]
    if budget is not None:
        lines.append(
            f"- 已用轮次：{budget.state.rounds}/{budget.state.max_rounds}；"
            f"模型调用：{budget.state.llm_calls}/{budget.state.max_llm_calls}；"
            f"token：{budget.state.input_tokens + budget.state.output_tokens}/{budget.state.max_tokens}"
        )
    lines += ["", "### 2. 未达标的评估指标", ""]
    failed = [
        (name, metric)
        for name, metric in (evaluation.metrics.items() if evaluation else [])
        if metric.value is None or metric.value < 0.8
    ]
    if failed:
        for name, metric in failed:
            label = _METRIC_LABELS.get(name, name)
            lines.append(f"- {label}：{_fmt_metric(metric.value)}（{metric.notes or '无说明'}）")
    else:
        lines.append("- 无可用的分项评估数据")

    lines += ["", "### 3. 未解决的研究任务", ""]
    unresolved = [t for t in state.plan if t.status is not TaskStatus.DONE]
    if unresolved:
        for task in unresolved:
            lines.append(f"- `{task.id}` {task.query} —— 目标：{task.goal}")
    else:
        lines.append("- 所有计划任务均已执行，但报告覆盖不足（见第 2 节）")

    lines += ["", "### 4. 报告已知薄弱点", ""]
    weak = (
        [c for c in (evaluation.claims if evaluation else []) if c.support is not SupportLevel.SUPPORTED]
        if evaluation
        else []
    )
    if weak:
        for claim in weak[:10]:
            lines.append(f"- [{claim.support.value}] {claim.text[:120]}")
    else:
        lines.append("- 本次未能完成结论级校验，薄弱点未知")

    lines += ["", "### 5. 建议的下一步", ""]
    if state.open_questions:
        for question in state.open_questions[:10]:
            lines.append(f"- {question}")
    else:
        lines.append("- 增加预算后重跑，或补充人工检索以覆盖上述未解决问题")
    return "\n".join(lines)
