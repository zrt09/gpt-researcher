"""Adaptive research loop (experimental, disabled by default).

Public entry point will be :class:`AdaptiveResearcher`; until it lands, this
package exposes the state/budget primitives it is built from.
"""

from .state import (
    BudgetState,
    Claim,
    Deficit,
    EvaluationResult,
    Evidence,
    MetricResult,
    PlanSnapshot,
    ReportVersion,
    ResearchState,
    RoundRecord,
    RunStatus,
    StopReason,
    SubTask,
    SupportLevel,
    TaskOrigin,
    TaskStatus,
)

__all__ = [
    "BudgetState",
    "Claim",
    "Deficit",
    "EvaluationResult",
    "Evidence",
    "MetricResult",
    "PlanSnapshot",
    "ReportVersion",
    "ResearchState",
    "RoundRecord",
    "RunStatus",
    "StopReason",
    "SubTask",
    "SupportLevel",
    "TaskOrigin",
    "TaskStatus",
    "Usage",
]
