"""Final report assembly.

The report body comes from the writer; this module only appends the quality
self-check. When the run did not meet the quality gate, the appendix is the
machine-enforced form of "never fake completion": it must list the stop reason,
the failing metrics, the unresolved tasks, the weak points and a next step.

The appendix follows the report's language so a report never mixes scripts.
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

_LABELS = {
    "en": {
        "appendix": "Appendix: Quality Self-Check",
        "passed": "This run met every quality gate.",
        "status": "**Run status**",
        "status_text": {
            "completed": "Completed (all quality gates met)",
            "partial": "Partial (open issues remain, see below)",
            "failed": "Failed (research could not complete)",
        },
        "stop": "### 1. Stop reason and resource usage",
        "stop_label": "Stop reason",
        "budget": "Rounds",
        "metrics": "### 2. Failing quality metrics",
        "no_metrics": "No usable metric data",
        "tasks": "### 3. Unresolved research tasks",
        "all_tasks_done": "All planned tasks ran, but the report under-covers the question (see section 2)",
        "weak": "### 4. Known weak points",
        "no_weak": "Claim-level checks did not run, so weak points are unknown",
        "next": "### 5. Suggested next steps",
        "next_default": "Re-run with a larger budget, or fill the gaps above with manual research",
        "table_head": ["| Metric | Result | Detail |", "| --- | --- | --- |"],
        "no_eval": "| (no evaluation data) | - | - |",
        "unknown": "unknown",
        "not_computable": "n/a",
    },
    "zh": {
        "appendix": "附录：研究质量自检",
        "passed": "本次研究已通过全部质量门槛。",
        "status": "**运行状态**",
        "status_text": {
            "completed": "已完成（质量门槛全部满足）",
            "partial": "部分完成（存在未解决的问题，见下）",
            "failed": "未完成（研究过程失败）",
        },
        "stop": "### 1. 停止原因与资源使用",
        "stop_label": "停止原因",
        "budget": "已用轮次",
        "metrics": "### 2. 未达标的评估指标",
        "no_metrics": "无可用分项数据",
        "tasks": "### 3. 未解决的研究任务",
        "all_tasks_done": "所有计划任务均已执行，但报告覆盖不足（见第 2 节）",
        "weak": "### 4. 报告已知薄弱点",
        "no_weak": "本次未能完成结论级校验，薄弱点未知",
        "next": "### 5. 建议的下一步",
        "next_default": "增加预算后重跑，或补充人工检索以覆盖上述未解决问题",
        "table_head": ["| 指标 | 结果 | 说明 |", "| --- | --- | --- |"],
        "no_eval": "| （无评估数据） | - | - |",
        "unknown": "未知",
        "not_computable": "无法计算",
    },
}

_METRIC_LABELS = {
    "en": {
        "coverage": "Question coverage",
        "citation_match_url": "Resolvable citations",
        "support_rate": "Claim support rate",
        "citation_match_semantic": "Citation-claim match",
        "source_validity": "Source validity",
        "source_freshness": "Source freshness",
        "conflict_rate": "Contradiction rate",
        "completeness": "Report completeness",
    },
    "zh": {
        "coverage": "研究问题覆盖度",
        "citation_match_url": "引用可回溯率",
        "support_rate": "关键结论证据支持率",
        "citation_match_semantic": "引用支撑结论率",
        "source_validity": "来源有效性",
        "source_freshness": "来源时效性",
        "conflict_rate": "结论冲突率",
        "completeness": "结果完整性",
    },
}


def _labels(language: str) -> dict:
    return _LABELS.get(language, _LABELS["en"])


def _metric_label(name: str, language: str) -> str:
    table = _METRIC_LABELS.get(language, _METRIC_LABELS["en"])
    return table.get(name, name)


def _fmt_metric(value: float | None, language: str) -> str:
    return _labels(language)["not_computable"] if value is None else f"{value:.2f}"


def _metrics_table(evaluation: EvaluationResult | None, language: str) -> list[str]:
    labels = _labels(language)
    lines = list(labels["table_head"])
    if evaluation is None or not evaluation.metrics:
        lines.append(labels["no_eval"])
        return lines
    for name, metric in evaluation.metrics.items():
        detail = metric.notes or ""
        if metric.denominator:
            detail = f"{detail} ({metric.numerator}/{metric.denominator})".strip()
        lines.append(
            f"| {_metric_label(name, language)} | {_fmt_metric(metric.value, language)} | {detail} |"
        )
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
    language: str = "zh",
) -> str:
    """Append the self-check appendix, keeping References last."""
    language = language if language in _LABELS else "en"
    if status is RunStatus.COMPLETED:
        appendix = _completed_appendix(evaluation, language)
    else:
        appendix = _incomplete_appendix(state, evaluation, status, stop_reason, budget, language)
    return _insert_before_references(report, appendix)


def _completed_appendix(evaluation: EvaluationResult | None, language: str) -> str:
    labels = _labels(language)
    lines = [f"## {labels['appendix']}", "", labels["passed"], ""]
    lines += _metrics_table(evaluation, language)
    return "\n".join(lines)


def _incomplete_appendix(
    state: ResearchState,
    evaluation: EvaluationResult | None,
    status: RunStatus,
    stop_reason: StopReason | None,
    budget,
    language: str,
) -> str:
    labels = _labels(language)
    status_text = labels["status_text"].get(status.value, status.value)
    lines = [
        f"## {labels['appendix']}",
        "",
        f"{labels['status']}：{status_text}" if language == "zh" else f"{labels['status']}: {status_text}",
        "",
        labels["stop"],
        "",
        f"- {labels['stop_label']}: `{stop_reason.value if stop_reason else labels['unknown']}`",
    ]
    if budget is not None:
        lines.append(
            f"- {labels['budget']}: {budget.state.rounds}/{budget.state.max_rounds}；"
            f"LLM: {budget.state.llm_calls}/{budget.state.max_llm_calls}；"
            f"tokens: {budget.state.input_tokens + budget.state.output_tokens}/{budget.state.max_tokens}"
        )

    lines += ["", labels["metrics"], ""]
    failed = [
        (name, metric)
        for name, metric in (evaluation.metrics.items() if evaluation else [])
        if metric.value is None or metric.value < 0.8
    ]
    if failed:
        for name, metric in failed:
            lines.append(
                f"- {_metric_label(name, language)}: {_fmt_metric(metric.value, language)}"
                f"（{metric.notes or '-'}）" if language == "zh"
                else f"- {_metric_label(name, language)}: {_fmt_metric(metric.value, language)} ({metric.notes or '-'})"
            )
    else:
        lines.append(f"- {labels['no_metrics']}")

    lines += ["", labels["tasks"], ""]
    unresolved = [t for t in state.plan if t.status is not TaskStatus.DONE]
    if unresolved:
        for task in unresolved[:10]:
            lines.append(f"- `{task.id}` {task.query}")
    else:
        lines.append(f"- {labels['all_tasks_done']}")

    lines += ["", labels["weak"], ""]
    weak = (
        [c for c in (evaluation.claims if evaluation else []) if c.support is not SupportLevel.SUPPORTED]
        if evaluation
        else []
    )
    if weak:
        for claim in weak[:8]:
            lines.append(f"- [{claim.support.value}] {claim.text[:110]}")
    else:
        lines.append(f"- {labels['no_weak']}")

    lines += ["", labels["next"], ""]
    if state.open_questions:
        for question in state.open_questions[:8]:
            lines.append(f"- {question}")
    else:
        lines.append(f"- {labels['next_default']}")
    return "\n".join(lines)
