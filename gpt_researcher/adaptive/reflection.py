"""Reflection: turn one evaluation into exactly one next action.

Rules come first; the LLM is only needed later, in the planner, to turn gaps into
search queries. The decision is a pure function of (evaluation, state, budget) so
the whole table can be unit-tested without network access.

Priority order (docs/改造方案/09_动态决策与终止机制.md):

1. budget exhausted          -> finish
2. quality met               -> finish
3. no usable metrics         -> abort
4. no progress               -> finish
5. missing coverage (many)   -> replan
6. missing coverage          -> supplement
7. weak evidence             -> supplement (or rewrite when every claim is sourced)
8. broken citations          -> supplement
9. conflicts                 -> supplement
10. structure problems       -> rewrite
11. fallback                 -> finish (no progress)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

from .budget import BudgetController
from .state import (
    Deficit,
    EvaluationResult,
    ResearchState,
    RoundRecord,
    StopReason,
    SupportLevel,
    TaskOrigin,
    TaskStatus,
)

CORE_METRICS = ("coverage", "citation_match_url", "completeness")
PROGRESS_EPSILON = 0.02
REPLAN_MIN_MISSING = 2


class Action(str, Enum):
    SUPPLEMENT = "supplement"
    REPLAN = "replan"
    REWRITE = "rewrite"
    FINISH = "finish"
    ABORT = "abort"


@dataclass
class Decision:
    action: Action
    reason: str
    addressed: list[Deficit] = field(default_factory=list)
    rewrite_notes: list[str] = field(default_factory=list)
    stop_reason: StopReason | None = None


def _metric_value(record: RoundRecord, name: str) -> float | None:
    if record.evaluation is None:
        return None
    return record.evaluation.value(name)


def has_no_progress(state: ResearchState, window: int = 2) -> bool:
    """True when recent rounds added no evidence and improved nothing.

    "No open tasks" is deliberately NOT treated as no progress here: a plan can
    be fully executed and still under-cover the question, in which case the
    evaluator's deficits must drive a re-plan instead of an early stop.
    """
    recent = state.rounds[-window:]
    if len(recent) < window:
        return False

    no_new_evidence = all(r.new_evidence == 0 for r in recent)
    latest = recent[-1].evaluation
    previous = recent[-2].evaluation
    flat = True
    if latest is not None and previous is not None:
        for name in CORE_METRICS:
            current = latest.value(name)
            before = previous.value(name)
            if current is not None and before is not None and current - before >= PROGRESS_EPSILON:
                flat = False
                break
    return no_new_evidence and flat


class ReflectionEngine:
    """Maps an evaluation onto a next action."""

    def __init__(self, cfg=None, no_progress_rounds: int = 2) -> None:
        self.no_progress_rounds = int(
            getattr(cfg, "adaptive_no_progress_rounds", no_progress_rounds)
        )

    def decide(
        self,
        evaluation: EvaluationResult,
        state: ResearchState,
        budget: BudgetController,
    ) -> Decision:
        exhausted = budget.exhaust_reason()
        if exhausted is not None:
            return Decision(
                action=Action.FINISH,
                reason=f"预算触顶：{exhausted.value}",
                stop_reason=exhausted,
            )

        if evaluation.passed:
            return Decision(
                action=Action.FINISH,
                reason="质量达标",
                stop_reason=StopReason.QUALITY_MET,
            )

        if all(evaluation.value(name) is None for name in CORE_METRICS):
            return Decision(action=Action.ABORT, reason="评估不可用：核心指标均无法计算")

        if has_no_progress(state, self.no_progress_rounds):
            return Decision(
                action=Action.FINISH,
                reason=f"连续 {self.no_progress_rounds} 轮无进展",
                stop_reason=StopReason.NO_PROGRESS,
            )

        deficits = evaluation.deficits
        missing = [d for d in deficits if d.kind == "missing_coverage"]
        weak = [d for d in deficits if d.kind == "weak_evidence"]
        broken = [d for d in deficits if d.kind == "broken_citation"]
        conflicts = [d for d in deficits if d.kind == "conflict"]
        structure = [d for d in deficits if d.kind == "incomplete_section"]

        if missing and self._looks_like_writing_gap(missing, state):
            return Decision(
                action=Action.REWRITE,
                reason="覆盖缺口出现在已完成任务上，属于写作侧问题，先重写而不是继续检索",
                addressed=missing,
                rewrite_notes=[d.description for d in missing],
            )

        if self._needs_replan(missing, state):
            return Decision(
                action=Action.REPLAN,
                reason=f"{len(missing)} 个首轮任务未被覆盖，计划需要重排",
                addressed=missing,
            )

        if missing:
            return Decision(
                action=Action.SUPPLEMENT,
                reason=f"{len(missing)} 个子任务缺少覆盖",
                addressed=missing,
            )

        if broken:
            return Decision(
                action=Action.SUPPLEMENT,
                reason=f"{len(broken)} 个引用无法回溯",
                addressed=broken,
            )

        if conflicts:
            return Decision(
                action=Action.SUPPLEMENT,
                reason=f"{len(conflicts)} 处结论冲突需要交叉验证",
                addressed=conflicts,
            )

        if weak:
            citation = evaluation.value("citation_match_url")
            if citation is not None and citation >= 0.9:
                return Decision(
                    action=Action.REWRITE,
                    reason="证据可回溯但表述超出证据，需要收紧结论",
                    addressed=weak,
                    rewrite_notes=[d.description for d in weak],
                )
            return Decision(
                action=Action.SUPPLEMENT,
                reason=f"{len(weak)} 条结论缺少证据",
                addressed=weak,
            )

        if structure:
            return Decision(
                action=Action.REWRITE,
                reason="报告结构不完整",
                addressed=structure,
                rewrite_notes=[d.description for d in structure],
            )

        return Decision(
            action=Action.FINISH,
            reason="无缺陷可处理且未达标，停止以免空转",
            stop_reason=StopReason.NO_PROGRESS,
        )

    @staticmethod
    def _looks_like_writing_gap(missing: Sequence[Deficit], state: ResearchState) -> bool:
        """All missing-coverage tasks already ran but did not make it into the text."""
        if not missing:
            return False
        for deficit in missing:
            task = state.task_by_id(deficit.task_id) if deficit.task_id else None
            if task is None or task.status is not TaskStatus.DONE:
                return False
        return True

    @staticmethod
    def _needs_replan(missing: Sequence[Deficit], state: ResearchState) -> bool:
        if len(missing) < REPLAN_MIN_MISSING:
            return False
        initial_failed = 0
        for deficit in missing:
            task = state.task_by_id(deficit.task_id) if deficit.task_id else None
            if task is None:
                continue
            if task.origin is TaskOrigin.INITIAL and task.attempts >= 1:
                initial_failed += 1
        return initial_failed >= REPLAN_MIN_MISSING


def unsupported_claims(evaluation: EvaluationResult) -> list[str]:
    """Claims explicitly judged unsupported - used by the rewrite path."""
    return [
        claim.id for claim in evaluation.claims if claim.support is SupportLevel.UNSUPPORTED
    ]


def open_task_ids(state: ResearchState) -> list[str]:
    return [t.id for t in state.plan if t.status is TaskStatus.PENDING]
