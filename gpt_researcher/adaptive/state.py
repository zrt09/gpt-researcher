"""Data model for the adaptive research loop.

This module defines the single source of truth for one adaptive research run:
the plan, the evidence collected, the claims extracted from the report, the
per-round evaluation, and the budget/termination bookkeeping.

Design notes (see docs/改造方案/07_State数据结构设计.md):

* Plain ``dataclasses`` - no pydantic dependency, cheap ``asdict``/JSON.
* Every object round-trips through ``to_dict()`` / ``from_dict()``.
* ``ResearchState.validate()`` encodes the invariants that make "never fake
  completion" machine-checkable, not just a documentation convention.
* The module never imports the rest of the package, so it can be unit-tested
  with zero network/LLM access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Hard limits (keeps a long run's state.json bounded)
# ---------------------------------------------------------------------------

MAX_EVIDENCE = 120
MAX_CLAIMS = 200
MAX_PLAN_HISTORY = 20
MAX_SNIPPET_WORDS = 400
MAX_OPEN_QUESTIONS = 50


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskOrigin(str, Enum):
    INITIAL = "initial"
    GAP = "gap"
    REPLAN = "replan"


class SupportLevel(str, Enum):
    SUPPORTED = "supported"
    WEAK = "weak"
    UNSUPPORTED = "unsupported"
    UNCHECKED = "unchecked"


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class StopReason(str, Enum):
    QUALITY_MET = "quality_met"
    MAX_ROUNDS = "max_rounds"
    MAX_LLM_CALLS = "max_llm_calls"
    MAX_TOKENS = "max_tokens"
    MAX_SECONDS = "max_seconds"
    MAX_COST = "max_cost"
    NO_PROGRESS = "no_progress"
    PLANNER_EXHAUSTED = "planner_exhausted"
    ERROR = "error"


def _as_dict(value: Any) -> Any:
    """Convert dataclasses/enums/sets into JSON-safe structures."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, (list, tuple)):
        return [_as_dict(v) for v in value]
    if isinstance(value, dict):
        return {k: _as_dict(v) for k, v in value.items()}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


@dataclass
class SubTask:
    """One research sub-question with a verifiable goal."""

    id: str
    query: str
    goal: str
    origin: TaskOrigin = TaskOrigin.INITIAL
    parent_id: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    evidence_ids: list[str] = field(default_factory=list)
    created_round: int = 1
    closed_round: int | None = None
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "query": self.query,
            "goal": self.goal,
            "origin": self.origin.value,
            "parent_id": self.parent_id,
            "status": self.status.value,
            "attempts": self.attempts,
            "evidence_ids": list(self.evidence_ids),
            "created_round": self.created_round,
            "closed_round": self.closed_round,
            "failure_reason": self.failure_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SubTask":
        return cls(
            id=d["id"],
            query=d["query"],
            goal=d.get("goal", ""),
            origin=TaskOrigin(d.get("origin", TaskOrigin.INITIAL.value)),
            parent_id=d.get("parent_id"),
            status=TaskStatus(d.get("status", TaskStatus.PENDING.value)),
            attempts=int(d.get("attempts", 0)),
            evidence_ids=list(d.get("evidence_ids") or []),
            created_round=int(d.get("created_round", 1)),
            closed_round=d.get("closed_round"),
            failure_reason=d.get("failure_reason"),
        )

    def is_open(self) -> bool:
        return self.status is TaskStatus.PENDING


@dataclass
class Evidence:
    """A compressed snippet of a fetched source, tied to the task that found it."""

    id: str
    task_id: str
    url: str
    title: str
    snippet: str
    query: str
    round: int
    fetched_at: str
    published_at: str | None = None
    source_kind: str = "web"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "query": self.query,
            "round": self.round,
            "fetched_at": self.fetched_at,
            "published_at": self.published_at,
            "source_kind": self.source_kind,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Evidence":
        return cls(
            id=d["id"],
            task_id=d["task_id"],
            url=d.get("url", ""),
            title=d.get("title", ""),
            snippet=d.get("snippet", ""),
            query=d.get("query", ""),
            round=int(d.get("round", 1)),
            fetched_at=d.get("fetched_at", ""),
            published_at=d.get("published_at"),
            source_kind=d.get("source_kind", "web"),
        )


@dataclass
class Claim:
    """A conclusion sentence lifted verbatim from the report."""

    id: str
    text: str
    section: str
    cited_urls: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    support: SupportLevel = SupportLevel.UNCHECKED
    judge_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "section": self.section,
            "cited_urls": list(self.cited_urls),
            "task_ids": list(self.task_ids),
            "evidence_ids": list(self.evidence_ids),
            "support": self.support.value,
            "judge_reason": self.judge_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Claim":
        return cls(
            id=d["id"],
            text=d.get("text", ""),
            section=d.get("section", ""),
            cited_urls=list(d.get("cited_urls") or []),
            task_ids=list(d.get("task_ids") or []),
            evidence_ids=list(d.get("evidence_ids") or []),
            support=SupportLevel(d.get("support", SupportLevel.UNCHECKED.value)),
            judge_reason=d.get("judge_reason"),
        )


@dataclass
class MetricResult:
    """One metric's value plus everything needed to audit how it was derived."""

    name: str
    value: float | None
    numerator: float | None = None
    denominator: float | None = None
    method: str = "deterministic"
    sampled: bool = False
    notes: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "method": self.method,
            "sampled": self.sampled,
            "notes": self.notes,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MetricResult":
        return cls(
            name=d["name"],
            value=d.get("value"),
            numerator=d.get("numerator"),
            denominator=d.get("denominator"),
            method=d.get("method", "deterministic"),
            sampled=bool(d.get("sampled", False)),
            notes=d.get("notes", ""),
            error=d.get("error"),
        )


@dataclass
class Deficit:
    """A concrete gap that can be turned into a research task or a rewrite."""

    kind: str
    description: str
    task_id: str | None = None
    claim_id: str | None = None
    suggested_query: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "description": self.description,
            "task_id": self.task_id,
            "claim_id": self.claim_id,
            "suggested_query": self.suggested_query,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Deficit":
        return cls(
            kind=d.get("kind", ""),
            description=d.get("description", ""),
            task_id=d.get("task_id"),
            claim_id=d.get("claim_id"),
            suggested_query=d.get("suggested_query"),
        )


@dataclass
class EvaluationResult:
    round: int
    mode: str
    metrics: dict[str, MetricResult] = field(default_factory=dict)
    claims: list[Claim] = field(default_factory=list)
    deficits: list[Deficit] = field(default_factory=list)
    passed: bool = False
    evaluated_at: str = ""
    llm_calls: int = 0

    def metric(self, name: str) -> MetricResult | None:
        return self.metrics.get(name)

    def value(self, name: str) -> float | None:
        m = self.metrics.get(name)
        return None if m is None else m.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "mode": self.mode,
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
            "claims": [c.to_dict() for c in self.claims],
            "deficits": [d.to_dict() for d in self.deficits],
            "passed": self.passed,
            "evaluated_at": self.evaluated_at,
            "llm_calls": self.llm_calls,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvaluationResult":
        return cls(
            round=int(d.get("round", 1)),
            mode=d.get("mode", "light"),
            metrics={
                k: MetricResult.from_dict(v) for k, v in (d.get("metrics") or {}).items()
            },
            claims=[Claim.from_dict(c) for c in (d.get("claims") or [])],
            deficits=[Deficit.from_dict(x) for x in (d.get("deficits") or [])],
            passed=bool(d.get("passed", False)),
            evaluated_at=d.get("evaluated_at", ""),
            llm_calls=int(d.get("llm_calls", 0)),
        )


@dataclass
class PlanSnapshot:
    version: int
    round: int
    task_ids: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "round": self.round,
            "task_ids": list(self.task_ids),
            "added": list(self.added),
            "removed": list(self.removed),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlanSnapshot":
        return cls(
            version=int(d.get("version", 1)),
            round=int(d.get("round", 1)),
            task_ids=list(d.get("task_ids") or []),
            added=list(d.get("added") or []),
            removed=list(d.get("removed") or []),
            reason=d.get("reason", ""),
        )


@dataclass
class ReportVersion:
    round: int
    path: str
    chars: int
    sha256: str
    decision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "path": self.path,
            "chars": self.chars,
            "sha256": self.sha256,
            "decision": self.decision,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReportVersion":
        return cls(
            round=int(d.get("round", 1)),
            path=d.get("path", ""),
            chars=int(d.get("chars", 0)),
            sha256=d.get("sha256", ""),
            decision=d.get("decision"),
        )


@dataclass
class RoundRecord:
    round: int
    plan_version: int = 1
    tasks_started: list[str] = field(default_factory=list)
    tasks_done: list[str] = field(default_factory=list)
    tasks_failed: list[str] = field(default_factory=list)
    new_evidence: int = 0
    new_claims: int = 0
    evaluation: EvaluationResult | None = None
    decision: str | None = None
    decision_reason: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "plan_version": self.plan_version,
            "tasks_started": list(self.tasks_started),
            "tasks_done": list(self.tasks_done),
            "tasks_failed": list(self.tasks_failed),
            "new_evidence": self.new_evidence,
            "new_claims": self.new_claims,
            "evaluation": None if self.evaluation is None else self.evaluation.to_dict(),
            "decision": self.decision,
            "decision_reason": self.decision_reason,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "elapsed_s": self.elapsed_s,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RoundRecord":
        ev = d.get("evaluation")
        return cls(
            round=int(d.get("round", 1)),
            plan_version=int(d.get("plan_version", 1)),
            tasks_started=list(d.get("tasks_started") or []),
            tasks_done=list(d.get("tasks_done") or []),
            tasks_failed=list(d.get("tasks_failed") or []),
            new_evidence=int(d.get("new_evidence", 0)),
            new_claims=int(d.get("new_claims", 0)),
            evaluation=None if ev is None else EvaluationResult.from_dict(ev),
            decision=d.get("decision"),
            decision_reason=d.get("decision_reason"),
            tokens_in=int(d.get("tokens_in", 0)),
            tokens_out=int(d.get("tokens_out", 0)),
            cost_usd=float(d.get("cost_usd", 0.0)),
            elapsed_s=float(d.get("elapsed_s", 0.0)),
        )


@dataclass
class BudgetState:
    """Limits plus the running counters; mutated in place by BudgetController."""

    max_rounds: int = 3
    max_llm_calls: int = 120
    max_tokens: int = 300_000
    max_seconds: int = 900
    max_cost_usd: float = 0.15
    rounds: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    first_exhausted: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_rounds": self.max_rounds,
            "max_llm_calls": self.max_llm_calls,
            "max_tokens": self.max_tokens,
            "max_seconds": self.max_seconds,
            "max_cost_usd": self.max_cost_usd,
            "rounds": self.rounds,
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "elapsed_s": self.elapsed_s,
            "first_exhausted": self.first_exhausted,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BudgetState":
        return cls(
            max_rounds=int(d.get("max_rounds", 3)),
            max_llm_calls=int(d.get("max_llm_calls", 120)),
            max_tokens=int(d.get("max_tokens", 300_000)),
            max_seconds=int(d.get("max_seconds", 900)),
            max_cost_usd=float(d.get("max_cost_usd", 0.15)),
            rounds=int(d.get("rounds", 0)),
            llm_calls=int(d.get("llm_calls", 0)),
            input_tokens=int(d.get("input_tokens", 0)),
            output_tokens=int(d.get("output_tokens", 0)),
            cost_usd=float(d.get("cost_usd", 0.0)),
            elapsed_s=float(d.get("elapsed_s", 0.0)),
            first_exhausted=d.get("first_exhausted"),
        )


@dataclass
class ResearchState:
    """Single source of truth for one adaptive research run."""

    run_id: str
    query: str
    report_type: str = "research_report"
    started_at: str = ""
    plan: list[SubTask] = field(default_factory=list)
    plan_version: int = 1
    plan_history: list[PlanSnapshot] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    visited_urls: set[str] = field(default_factory=set)
    rounds: list[RoundRecord] = field(default_factory=list)
    latest_evaluation: EvaluationResult | None = None
    report: str | None = None
    report_versions: list[ReportVersion] = field(default_factory=list)
    budget: BudgetState = field(default_factory=BudgetState)
    status: RunStatus = RunStatus.RUNNING
    stop_reason: StopReason | None = None
    open_questions: list[str] = field(default_factory=list)
    updated_at: str = ""

    # ---------------------------------------------------------------- queries
    def open_tasks(self) -> list[SubTask]:
        return [t for t in self.plan if t.is_open()]

    def task_by_id(self, task_id: str) -> SubTask | None:
        for task in self.plan:
            if task.id == task_id:
                return task
        return None

    def evidence_by_ids(self, ids: Iterable[str]) -> list[Evidence]:
        wanted = set(ids)
        return [e for e in self.evidence if e.id in wanted]

    def evidence_by_url(self, url: str) -> list[Evidence]:
        return [e for e in self.evidence if e.url == url]

    def evidence_urls(self) -> set[str]:
        return {e.url for e in self.evidence if e.url}

    # ------------------------------------------------------------ invariants
    def validate(self) -> None:
        """Raise ValueError when the state violates a documented invariant."""
        self._check_unique_ids()
        task_ids = {t.id for t in self.plan}
        for ev in self.evidence:
            if ev.task_id not in task_ids:
                raise ValueError(
                    f"evidence {ev.id} references unknown task {ev.task_id}"
                )
        evidence_ids = {e.id for e in self.evidence}
        for claim in self.claims:
            missing = [i for i in claim.evidence_ids if i not in evidence_ids]
            if missing:
                raise ValueError(
                    f"claim {claim.id} references unknown evidence {missing}"
                )

        if self.status is RunStatus.COMPLETED:
            if self.stop_reason is not StopReason.QUALITY_MET:
                raise ValueError(
                    "status=completed requires stop_reason=quality_met "
                    f"(got {self.stop_reason})"
                )
            if self.latest_evaluation is None or not self.latest_evaluation.passed:
                raise ValueError(
                    "status=completed requires a passing evaluation"
                )
        elif self.status in (RunStatus.PARTIAL, RunStatus.FAILED):
            if not self.open_questions and self.stop_reason is None:
                raise ValueError(
                    f"status={self.status.value} requires open_questions or stop_reason"
                )

        if self.budget.rounds != len(self.rounds):
            raise ValueError(
                f"budget.rounds={self.budget.rounds} != len(rounds)={len(self.rounds)}"
            )
        expected_version = len(self.plan_history) + (1 if self.plan else 0)
        if self.plan_version != expected_version:
            raise ValueError(
                f"plan_version={self.plan_version} != expected {expected_version}"
            )

    def _check_unique_ids(self) -> None:
        seen: set[str] = set()
        for task in self.plan:
            if task.id in seen:
                raise ValueError(f"duplicate task id {task.id}")
            seen.add(task.id)
        seen = set()
        for ev in self.evidence:
            if ev.id in seen:
                raise ValueError(f"duplicate evidence id {ev.id}")
            seen.add(ev.id)

    # --------------------------------------------------------------- trimming
    def enforce_limits(self) -> None:
        """Apply the documented caps instead of letting state.json grow forever."""
        for ev in self.evidence:
            if ev.snippet and len(ev.snippet.split()) > MAX_SNIPPET_WORDS:
                ev.snippet = " ".join(ev.snippet.split()[:MAX_SNIPPET_WORDS])
        if len(self.evidence) > MAX_EVIDENCE:
            keep = sorted(self.evidence, key=lambda e: e.round, reverse=True)[:MAX_EVIDENCE]
            self.evidence = sorted(keep, key=lambda e: e.round)
        if len(self.claims) > MAX_CLAIMS:
            self.claims = self.claims[:MAX_CLAIMS]
        if len(self.plan_history) > MAX_PLAN_HISTORY:
            self.plan_history = self.plan_history[-MAX_PLAN_HISTORY:]
        if len(self.open_questions) > MAX_OPEN_QUESTIONS:
            self.open_questions = self.open_questions[:MAX_OPEN_QUESTIONS]

    # ------------------------------------------------------------ (de)serialize
    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "query": self.query,
            "report_type": self.report_type,
            "started_at": self.started_at,
            "plan": [t.to_dict() for t in self.plan],
            "plan_version": self.plan_version,
            "plan_history": [p.to_dict() for p in self.plan_history],
            "evidence": [e.to_dict() for e in self.evidence],
            "claims": [c.to_dict() for c in self.claims],
            "visited_urls": sorted(self.visited_urls),
            "rounds": [r.to_dict() for r in self.rounds],
            "latest_evaluation": (
                None if self.latest_evaluation is None else self.latest_evaluation.to_dict()
            ),
            "report": self.report,
            "report_versions": [v.to_dict() for v in self.report_versions],
            "budget": self.budget.to_dict(),
            "status": self.status.value,
            "stop_reason": None if self.stop_reason is None else self.stop_reason.value,
            "open_questions": list(self.open_questions),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ResearchState":
        latest = d.get("latest_evaluation")
        stop = d.get("stop_reason")
        return cls(
            run_id=d["run_id"],
            query=d["query"],
            report_type=d.get("report_type", "research_report"),
            started_at=d.get("started_at", ""),
            plan=[SubTask.from_dict(t) for t in (d.get("plan") or [])],
            plan_version=int(d.get("plan_version", 1)),
            plan_history=[PlanSnapshot.from_dict(p) for p in (d.get("plan_history") or [])],
            evidence=[Evidence.from_dict(e) for e in (d.get("evidence") or [])],
            claims=[Claim.from_dict(c) for c in (d.get("claims") or [])],
            visited_urls=set(d.get("visited_urls") or []),
            rounds=[RoundRecord.from_dict(r) for r in (d.get("rounds") or [])],
            latest_evaluation=None if latest is None else EvaluationResult.from_dict(latest),
            report=d.get("report"),
            report_versions=[
                ReportVersion.from_dict(v) for v in (d.get("report_versions") or [])
            ],
            budget=BudgetState.from_dict(d.get("budget") or {}),
            status=RunStatus(d.get("status", RunStatus.RUNNING.value)),
            stop_reason=None if stop is None else StopReason(stop),
            open_questions=list(d.get("open_questions") or []),
            updated_at=d.get("updated_at", ""),
        )

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, text: str) -> "ResearchState":
        return cls.from_dict(json.loads(text))
