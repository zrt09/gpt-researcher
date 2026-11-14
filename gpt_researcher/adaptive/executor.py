"""Adapter that turns one plan task into structured evidence.

The executor deliberately wraps :class:`GPTResearcher` instead of re-running the
search/scrape/compress pipeline itself: one sub-task = one researcher instance
that shares ``visited_urls`` with its siblings, so cross-task duplicate fetching
keeps working exactly as in the original project.

Evidence is captured through the ``artifact_sink`` hook added to
``ResearchConductor``. Failures surface as ``TaskResult.status == FAILED`` with a
reason, never as a silently empty success.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..config import Config
from ..utils.enum import ReportSource, ReportType
from ..utils.llm import reset_active_usage_sink, set_active_usage_sink
from .state import Evidence, SubTask, TaskStatus
from .storage import utc_now

logger = logging.getLogger(__name__)

MAX_EVIDENCE_PER_TASK = 8
SNIPPET_MAX_CHARS = 1200
DEFAULT_TIMEOUT_S = 180.0


@dataclass
class TaskResult:
    """Outcome of executing one sub-task."""

    task_id: str
    status: TaskStatus
    evidence: list[Evidence] = field(default_factory=list)
    context: str = ""
    sources: list[dict] = field(default_factory=list)
    tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None


class ResearchExecutor:
    """Runs plan tasks through the original research pipeline."""

    def __init__(
        self,
        cfg: Config | None = None,
        config_path: str | None = None,
        shared_visited_urls: set[str] | None = None,
        usage_sink: Callable[..., None] | None = None,
        websocket: Any | None = None,
        researcher_factory: Callable[..., Any] | None = None,
        timeout_s: float | None = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.shared_visited_urls = shared_visited_urls if shared_visited_urls is not None else set()
        self.usage_sink = usage_sink
        self.websocket = websocket
        self.researcher_factory = researcher_factory or self._default_factory
        self.timeout_s = timeout_s
        self._evidence_seq = 0
        self._seen_urls: set[str] = set()
        self._tokens = 0
        self._cost = 0.0

    # ------------------------------------------------------------------ public
    async def run_task(
        self,
        task: SubTask,
        *,
        round_no: int = 1,
        agent: str | None = None,
        role: str | None = None,
    ) -> TaskResult:
        captured: list[dict[str, Any]] = []

        def sink(*, sub_query, pages, compressed, stage):
            captured.append(
                {
                    "sub_query": sub_query,
                    "pages": list(pages or []),
                    "compressed": compressed,
                    "stage": stage,
                }
            )

        tokens_before, cost_before = self._tokens, self._cost
        try:
            researcher = self.researcher_factory(
                task=task,
                config_path=self.config_path,
                visited_urls=self.shared_visited_urls,
                agent=agent,
                role=role,
                websocket=self.websocket,
                cfg=self.cfg,
            )
            # Plain attribute: the public GPTResearcher signature stays frozen.
            researcher.artifact_sink = sink
        except Exception as exc:  # noqa: BLE001 - construction failure is a task failure
            return self._failure(task, f"researcher init failed: {exc}")

        token = set_active_usage_sink(self._record_usage)
        try:
            if self.timeout_s:
                context = await asyncio.wait_for(
                    researcher.conduct_research(), timeout=self.timeout_s
                )
            else:
                context = await researcher.conduct_research()
        except asyncio.TimeoutError:
            return self._failure(task, f"timeout after {self.timeout_s:.0f}s")
        except Exception as exc:  # noqa: BLE001 - one task must not kill the round
            logger.warning("task %s failed: %s", task.id, exc)
            return self._failure(task, str(exc))
        finally:
            reset_active_usage_sink(token)

        context_text = self._as_text(context)
        evidence = self._build_evidence(task, captured, round_no)
        error = self._last_error(researcher)

        if not context_text.strip() and not evidence:
            reason = str(error) if error is not None else "empty context and no evidence"
            return self._failure(task, reason)

        if error is not None and not context_text.strip():
            return self._failure(task, str(error))

        sources = [s for s in getattr(researcher, "research_sources", []) or [] if isinstance(s, dict)]
        return TaskResult(
            task_id=task.id,
            status=TaskStatus.DONE,
            evidence=evidence,
            context=context_text,
            sources=sources,
            tokens=self._tokens - tokens_before,
            cost_usd=self._cost - cost_before,
        )

    # ----------------------------------------------------------------- helpers
    def _failure(self, task: SubTask, message: str) -> TaskResult:
        return TaskResult(
            task_id=task.id,
            status=TaskStatus.FAILED,
            error=message[:500],
            tokens=self._tokens,
            cost_usd=self._cost,
        )

    @staticmethod
    def _as_text(context: Any) -> str:
        if context is None:
            return ""
        if isinstance(context, str):
            return context
        if isinstance(context, (list, tuple)):
            return " ".join(str(item) for item in context)
        return str(context)

    @staticmethod
    def _last_error(researcher: Any) -> Exception | None:
        conductor = getattr(researcher, "research_conductor", None)
        return getattr(conductor, "last_error", None) if conductor is not None else None

    def _record_usage(self, **usage: Any) -> None:
        try:
            self._tokens += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
            self._cost += float(usage.get("cost_usd", 0.0))
        except (TypeError, ValueError):
            logger.warning("unexpected usage payload: %r", usage)
        if self.usage_sink is not None:
            try:
                self.usage_sink(**usage)
            except Exception as exc:  # noqa: BLE001
                logger.warning("usage_sink rejected payload: %s", exc)

    def _next_evidence_id(self) -> str:
        self._evidence_seq += 1
        return f"E{self._evidence_seq}"

    def _build_evidence(
        self, task: SubTask, captured: Sequence[dict[str, Any]], round_no: int
    ) -> list[Evidence]:
        """Turn captured pages into bounded evidence records (URL-deduplicated)."""
        evidence: list[Evidence] = []
        for item in captured:
            for page in item.get("pages") or []:
                if len(evidence) >= MAX_EVIDENCE_PER_TASK:
                    return evidence
                if not isinstance(page, dict):
                    continue
                url = page.get("url") or page.get("source") or ""
                if not url or url in self._seen_urls:
                    continue
                self._seen_urls.add(url)
                body = page.get("raw_content") or page.get("content") or ""
                evidence.append(
                    Evidence(
                        id=self._next_evidence_id(),
                        task_id=task.id,
                        url=url,
                        title=str(page.get("title") or "")[:200],
                        snippet=str(body)[:SNIPPET_MAX_CHARS].strip(),
                        query=task.query,
                        round=round_no,
                        fetched_at=utc_now(),
                        published_at=page.get("published_at"),
                        source_kind=str(page.get("source_kind") or "web"),
                    )
                )
        return evidence

    # ---------------------------------------------------------------- default
    def _default_factory(self, **kwargs: Any) -> Any:
        from ..agent import GPTResearcher

        task: SubTask = kwargs["task"]
        return GPTResearcher(
            query=task.query,
            report_type=ReportType.ResearchReport.value,
            report_source=ReportSource.Web.value,
            agent=kwargs.get("agent"),
            role=kwargs.get("role"),
            config_path=kwargs.get("config_path"),
            websocket=kwargs.get("websocket"),
            visited_urls=kwargs.get("visited_urls"),
        )
