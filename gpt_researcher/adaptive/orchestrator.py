"""The adaptive research loop.

Wires the planner, executor, evaluator, reflection and budget into rounds:

    plan -> execute -> write -> evaluate -> decide -> (supplement | replan | rewrite | finish)

Everything except the writer and the executor is injectable, which keeps the
loop testable without network access. The public entry point is
:class:`AdaptiveResearcher`; the original ``GPTResearcher`` path is untouched.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..config import Config
from ..observability import emit_research_event
from ..prompts import get_prompt_family
from ..utils.enum import ReportType, Tone
from .budget import BudgetController, Usage
from .evaluator import ResearchEvaluator, default_judge
from .executor import ResearchExecutor
from .planner import Planner
from .reflection import Action, ReflectionEngine
from .report import build_final_report
from .state import (
    BudgetState,
    EvaluationResult,
    PlanSnapshot,
    ResearchState,
    RoundRecord,
    RunStatus,
    StopReason,
    SubTask,
    TaskStatus,
)
from .storage import (
    make_run_id,
    run_dir,
    save_final_report,
    save_report_version,
    save_state,
    utc_now,
)

logger = logging.getLogger(__name__)

DEFAULT_ROLE_PROMPT = (
    "You are an expert research analyst. Write a rigorous, well-cited report "
    "that uses only the supplied evidence."
)

WriterCall = Callable[[ResearchState, list[str]], Awaitable[str]]


@dataclass
class AdaptiveResult:
    report: str
    evaluation: EvaluationResult | None
    state: ResearchState
    status: RunStatus
    stop_reason: StopReason | None
    directory: str | None = None


class AdaptiveResearcher:
    """One adaptive research run."""

    def __init__(
        self,
        query: str,
        *,
        cfg: Config | None = None,
        config_path: str | None = None,
        websocket: Any | None = None,
        observer: Any | None = None,
        planner: Planner | None = None,
        executor: Any | None = None,
        evaluator: ResearchEvaluator | None = None,
        reflection: ReflectionEngine | None = None,
        writer: WriterCall | None = None,
        max_rounds: int | None = None,
        eval_mode: str | None = None,
        state_dir: str | None = None,
        persist: bool = True,
        budget_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.cfg = cfg or Config(config_path)
        self.config_path = config_path
        self.query = query
        self.websocket = websocket
        self.observer = observer
        self.persist = persist
        self.eval_mode = eval_mode or str(getattr(self.cfg, "adaptive_eval_mode", "light"))

        budget = self._build_budget_state(budget_overrides or {}, max_rounds)
        self.state = ResearchState(
            run_id=make_run_id(query),
            query=query,
            report_type=ReportType.ResearchReport.value,
            started_at=utc_now(),
            budget=budget,
        )
        self.budget = BudgetController(self.state.budget)

        self.planner = planner or Planner(self.cfg)
        self.executor = executor or ResearchExecutor(
            cfg=self.cfg,
            config_path=config_path,
            shared_visited_urls=self.state.visited_urls,
            usage_sink=self._record_usage,
            websocket=websocket,
        )
        if evaluator is None and self.eval_mode == "full":
            evaluator = ResearchEvaluator(self.cfg, judge=default_judge(self.cfg))
        self.evaluator = evaluator or ResearchEvaluator(self.cfg)
        self.reflection = reflection or ReflectionEngine(self.cfg)

        self.prompt_family = get_prompt_family(
            getattr(self.cfg, "prompt_family", "default"), self.cfg
        )
        self.writer: WriterCall = writer or self._default_writer
        # A fixed role lets every sub-task skip its own choose_agent LLM call.
        # (One call per task is pure overhead when the parent already knows the
        # domain; the adaptive loop can still override these explicitly.)
        self.agent = "researcher"
        self.role = DEFAULT_ROLE_PROMPT
        self.max_tasks_per_round = int(
            getattr(self.cfg, "adaptive_max_tasks_per_round", 4)
        )
        self.concurrency = max(1, int(getattr(self.cfg, "adaptive_concurrency", 3)))
        self.state_dir = state_dir or str(getattr(self.cfg, "adaptive_state_dir", "outputs/adaptive"))
        self.directory: str | None = None
        self._pending_notes: list[str] = []
        self._writer_degraded = False

    # ------------------------------------------------------------------ public
    async def run(self, on_round: Callable[[RoundRecord], None] | None = None) -> AdaptiveResult:
        state = self.state
        if self.persist:
            self.directory = str(run_dir(self.state_dir, state.run_id))

        plan_ok = await self._initial_plan()
        if not plan_ok:
            return self._finish(None)

        while True:
            allowed, reason = self.budget.begin_round()
            if not allowed:
                state.stop_reason = reason
                break
            round_no = state.budget.rounds
            await self._emit("adaptive_round_started", round=round_no)

            tasks = state.open_tasks()[: self.max_tasks_per_round]
            started = [t.id for t in tasks]
            results = await self._run_tasks(tasks, round_no)
            new_evidence = self._apply_results(results, round_no)

            report = await self._write()
            state.report = report
            version = self._save_version(report, round_no)
            if version is not None:
                state.report_versions.append(version)

            evaluation = await self.evaluator.evaluate(
                state, report, mode=self.eval_mode, round_no=round_no
            )
            state.latest_evaluation = evaluation
            state.claims = list(evaluation.claims)

            record = RoundRecord(
                round=round_no,
                plan_version=state.plan_version,
                tasks_started=started,
                tasks_done=[r.task_id for r in results if r.status is TaskStatus.DONE],
                tasks_failed=[r.task_id for r in results if r.status is TaskStatus.FAILED],
                new_evidence=new_evidence,
                new_claims=len(evaluation.claims),
                evaluation=evaluation,
                tokens_in=state.budget.input_tokens,
                tokens_out=state.budget.output_tokens,
                cost_usd=state.budget.cost_usd,
            )
            state.rounds.append(record)

            stop = self._stop_reason(evaluation)
            if stop is not None:
                state.stop_reason = stop
                record.decision = Action.FINISH.value
                record.decision_reason = stop.value
                self._persist()
                on_round and on_round(record)
                break

            decision = self.reflection.decide(evaluation, state, self.budget)
            record.decision = decision.action.value
            record.decision_reason = decision.reason
            await self._emit(
                "reflection_decided",
                round=round_no,
                action=decision.action.value,
                reason=decision.reason,
            )

            if decision.action is Action.ABORT:
                state.stop_reason = StopReason.ERROR
                self._persist()
                break
            if decision.action is Action.FINISH:
                state.stop_reason = decision.stop_reason or StopReason.NO_PROGRESS
                self._persist()
                on_round and on_round(record)
                break
            if decision.action is Action.REWRITE:
                self._pending_notes = decision.rewrite_notes

            if decision.action in (Action.SUPPLEMENT, Action.REPLAN):
                await self._apply_revision(evaluation, round_no)

            if not state.open_tasks():
                state.stop_reason = StopReason.NO_PROGRESS
                self._persist()
                on_round and on_round(record)
                break

            self._persist()
            on_round and on_round(record)

        return self._finish(state.latest_evaluation)

    # ------------------------------------------------------------------ phases
    async def _initial_plan(self) -> bool:
        try:
            tasks = await self.planner.initial_plan(self.query)
        except Exception as exc:  # noqa: BLE001 - planning failure is terminal
            logger.error("adaptive planner failed: %s", exc)
            self.state.stop_reason = StopReason.PLANNER_EXHAUSTED
            self.state.status = RunStatus.FAILED
            self._persist()
            return False
        if not tasks:
            self.state.stop_reason = StopReason.PLANNER_EXHAUSTED
            self.state.status = RunStatus.FAILED
            self._persist()
            return False

        self.state.plan = tasks
        self.state.plan_version = 1
        self.state.plan_history.append(
            PlanSnapshot(
                version=1,
                round=0,
                task_ids=[t.id for t in tasks],
                added=[t.id for t in tasks],
                reason="initial plan",
            )
        )
        self._persist()
        return True

    async def _apply_revision(self, evaluation: EvaluationResult, round_no: int) -> None:
        try:
            new_tasks = await self.planner.revise(self.state, evaluation)
        except Exception as exc:  # noqa: BLE001 - revision failure is not fatal
            logger.warning("adaptive planner revision failed: %s", exc)
            return
        if not new_tasks:
            return
        self.state.plan.extend(new_tasks)
        self.state.plan_version += 1
        self.state.plan_history.append(
            PlanSnapshot(
                version=self.state.plan_version,
                round=round_no,
                task_ids=[t.id for t in self.state.plan],
                added=[t.id for t in new_tasks],
                reason="gap-driven revision",
            )
        )

    async def _run_tasks(self, tasks: list[SubTask], round_no: int) -> list[Any]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(task: SubTask):
            async with semaphore:
                return await self.executor.run_task(
                    task, round_no=round_no, agent=self.agent, role=self.role
                )

        results = await asyncio.gather(*[one(t) for t in tasks], return_exceptions=True)
        clean = []
        for task, result in zip(tasks, results):
            if isinstance(result, Exception):
                from .executor import TaskResult

                clean.append(
                    TaskResult(task_id=task.id, status=TaskStatus.FAILED, error=str(result))
                )
            else:
                clean.append(result)
        return clean

    def _apply_results(self, results: list[Any], round_no: int) -> int:
        state = self.state
        added = 0
        for result in results:
            task = state.task_by_id(result.task_id)
            if task is None:
                continue
            task.attempts += 1
            if result.status is TaskStatus.DONE:
                task.status = TaskStatus.DONE
                task.closed_round = round_no
                for evidence in result.evidence:
                    state.evidence.append(evidence)
                    task.evidence_ids.append(evidence.id)
                    if evidence.url:
                        state.visited_urls.add(evidence.url)
                    added += 1
            else:
                task.status = TaskStatus.FAILED
                task.closed_round = round_no
                task.failure_reason = result.error
        return added

    async def _write(self) -> str:
        try:
            report = await self.writer(self.state, list(self._pending_notes))
        except Exception as exc:  # noqa: BLE001 - keep the previous report
            logger.error("adaptive writer failed: %s", exc)
            report = self.state.report or ""
        if report.strip():
            return report
        # Never hand an empty report to the evaluator: a deterministic digest of
        # the collected evidence is degraded, but it is honest and it keeps the
        # run's stop reason meaningful.
        self._writer_degraded = True
        logger.warning("adaptive writer produced no text; using evidence digest")
        return self._digest_report(self.state)

    @staticmethod
    def _digest_report(state: ResearchState) -> str:
        lines = [
            f"# {state.query}",
            "",
            "## Introduction",
            "",
            "> 自动生成：写作模型本次不可用，以下为已收集证据的结构化摘要，未经撰写加工。",
            "",
        ]
        for task in state.plan:
            items = [e for e in state.evidence if e.task_id == task.id]
            if not items:
                continue
            lines += [f"## {task.goal}", ""]
            for item in items[:4]:
                lines.append(f"- {item.title or item.url}: {item.snippet[:400]} ({item.url})")
            lines.append("")
        lines += ["## Conclusion", "", "写作模型不可用，未能生成结论。", "", "## References", ""]
        urls = []
        for evidence in state.evidence:
            if evidence.url and evidence.url not in urls:
                urls.append(evidence.url)
        lines += [f"- {url}" for url in urls[:40]] or ["- （无）"]
        return "\n".join(lines)

    async def _default_writer(self, state: ResearchState, notes: list[str]) -> str:
        from ..actions.report_generation import generate_report

        attempts = ((24, 600), (10, 400))
        last_error: Exception | None = None
        for max_items, snippet_chars in attempts:
            context = self._evidence_context(
                state, notes, max_items=max_items, snippet_chars=snippet_chars
            )
            try:
                return await generate_report(
                    query=state.query,
                    context=context,
                    agent_role_prompt=DEFAULT_ROLE_PROMPT,
                    report_type=ReportType.ResearchReport.value,
                    tone=Tone.Objective,
                    report_source="web",
                    websocket=self.websocket,
                    cfg=self.cfg,
                    prompt_family=self.prompt_family,
                )
            except Exception as exc:  # noqa: BLE001 - retry smaller, then degrade
                last_error = exc
                logger.warning(
                    "adaptive writer attempt failed (max_items=%s): %s", max_items, exc
                )
        raise last_error or RuntimeError("writer failed")

    @staticmethod
    def _evidence_context(
        state: ResearchState,
        notes: list[str],
        max_items: int = 24,
        snippet_chars: int = 600,
    ) -> str:
        blocks: list[str] = []
        budget = max_items
        for task in state.plan:
            if budget <= 0:
                break
            items = [e for e in state.evidence if e.task_id == task.id][: max(1, budget // 4 or 1)]
            if not items:
                continue
            blocks.append(f"### {task.goal}")
            for item in items:
                budget -= 1
                blocks.append(
                    f"- {item.title} ({item.url}): {item.snippet[:snippet_chars]}"
                )
            if budget <= 0:
                break
        context = "\n".join(blocks) or "（暂无证据）"
        if notes:
            context += "\n\n修订要求（上一轮评估发现的问题）：\n" + "\n".join(
                f"- {note}" for note in notes
            )
        return context

    def _stop_reason(self, evaluation: EvaluationResult) -> StopReason | None:
        # Quality first: meeting the gate on the last allowed round must count as
        # success, not as "budget exhausted".
        if evaluation.passed:
            return StopReason.QUALITY_MET
        return self.budget.exhaust_reason()

    def _finish(self, evaluation: EvaluationResult | None) -> AdaptiveResult:
        state = self.state
        if not state.report:
            status = RunStatus.FAILED
            state.stop_reason = state.stop_reason or StopReason.ERROR
        elif state.stop_reason is StopReason.QUALITY_MET and evaluation and evaluation.passed:
            status = RunStatus.COMPLETED
        elif state.stop_reason in (StopReason.ERROR, StopReason.PLANNER_EXHAUSTED):
            status = RunStatus.FAILED
        else:
            status = RunStatus.PARTIAL

        state.status = status
        questions = self._open_questions(evaluation)
        if self._writer_degraded:
            if status is RunStatus.COMPLETED:
                status = RunStatus.PARTIAL
                state.status = status
            questions.insert(0, "写作模型不可用：最终报告为证据摘要，而非撰写结果")
        state.open_questions = questions
        final_report = build_final_report(
            state,
            state.report or "",
            evaluation,
            status,
            state.stop_reason,
            budget=self.budget,
        )
        if self.persist:
            if self.directory is None:
                self.directory = str(run_dir(self.state_dir, state.run_id))
            save_final_report(final_report, self.directory)
            save_state(state, self.directory)
        return AdaptiveResult(
            report=final_report,
            evaluation=evaluation,
            state=state,
            status=status,
            stop_reason=state.stop_reason,
            directory=self.directory,
        )

    def _open_questions(self, evaluation: EvaluationResult | None) -> list[str]:
        questions: list[str] = []
        if evaluation is not None:
            for deficit in evaluation.deficits:
                questions.append(f"{deficit.kind}: {deficit.description}")
        for task in self.state.plan:
            if task.status is not TaskStatus.DONE:
                questions.append(f"任务 {task.id} 未完成：{task.goal}")
        if self.state.stop_reason is not None:
            questions.append(f"停止原因：{self.state.stop_reason.value}")
        return questions[:20]

    # ----------------------------------------------------------------- helpers
    def _build_budget_state(
        self, overrides: dict[str, Any], max_rounds: int | None
    ) -> BudgetState:
        cfg = self.cfg
        return BudgetState(
            max_rounds=int(max_rounds or overrides.get("max_rounds") or getattr(cfg, "adaptive_max_rounds", 3)),
            max_llm_calls=int(overrides.get("max_llm_calls") or getattr(cfg, "adaptive_max_llm_calls", 120)),
            max_tokens=int(overrides.get("max_tokens") or getattr(cfg, "adaptive_max_tokens", 300_000)),
            max_seconds=int(overrides.get("max_seconds") or getattr(cfg, "adaptive_max_seconds", 900)),
            max_cost_usd=float(overrides.get("max_cost_usd") or getattr(cfg, "adaptive_max_cost_usd", 0.15)),
        )

    def _record_usage(self, **usage: Any) -> None:
        try:
            self.budget.record(
                Usage(
                    provider=str(usage.get("provider", "")),
                    model=str(usage.get("model", "")),
                    input_tokens=int(usage.get("input_tokens", 0) or 0),
                    output_tokens=int(usage.get("output_tokens", 0) or 0),
                    cost_usd=float(usage.get("cost_usd", 0.0) or 0.0),
                    estimated=bool(usage.get("estimated", False)),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("adaptive budget rejected usage payload: %s", exc)

    def _persist(self) -> None:
        if not self.persist or self.directory is None:
            return
        try:
            save_state(self.state, self.directory)
        except Exception as exc:  # noqa: BLE001 - persistence must not kill the run
            logger.warning("adaptive state save failed: %s", exc)

    def _save_version(self, report: str, round_no: int):
        if not self.persist or self.directory is None:
            return None
        try:
            return save_report_version(report, round_no, self.directory)
        except Exception as exc:  # noqa: BLE001
            logger.warning("adaptive report version save failed: %s", exc)
            return None

    async def _emit(self, event: str, **fields: Any) -> None:
        try:
            await emit_research_event(self.observer, event, **fields)
        except Exception:  # noqa: BLE001 - telemetry is optional
            logger.debug("adaptive observer failed for %s", event)
