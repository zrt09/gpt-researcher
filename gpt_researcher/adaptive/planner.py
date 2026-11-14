"""Adaptive planner: turn one research question into verifiable sub-tasks.

The planner is deliberately thin: it asks the strategic LLM for a small JSON
plan, validates it defensively, and falls back to a single research task rather
than failing the whole run. All network/LLM access goes through an injectable
``llm_call`` so unit tests run offline.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

import json_repair
from rapidfuzz import fuzz

from ..config import Config
from ..prompts import PromptFamily, get_prompt_family
from ..utils.llm import create_chat_completion
from .state import SubTask, TaskOrigin, TaskStatus

logger = logging.getLogger(__name__)

DEFAULT_MAX_TASKS = 6
DEDUPE_THRESHOLD = 85.0
# Ordered longest-first so "有哪些" is removed before "哪些".
_CJK_FILLERS = (
    "有哪些",
    "是什么",
    "为什么",
    "怎么样",
    "哪些",
    "哪些",
    "如何",
    "的",
    "了",
    "吗",
    "呢",
)
_FENCE_RE = re.compile(r"```(?:json)?\s*(?P<payload>[\s\S]*?)```", re.IGNORECASE)
_ARRAY_RE = re.compile(r"\[[\s\S]*\]")
_OBJECT_RE = re.compile(r"\{[\s\S]*\}")
_TASK_ID_RE = re.compile(r"^T(\d+)$")
_GAP_KINDS = ("missing_coverage", "weak_evidence", "broken_citation", "conflict")

LLMCall = Callable[[str], Awaitable[str]]


class Planner:
    """Builds the initial plan and revises it from evaluation gaps."""

    _MAX_GAP_LLM_CALLS = 3

    def __init__(
        self,
        cfg: Config | None = None,
        prompt_family: PromptFamily | None = None,
        llm_call: LLMCall | None = None,
        max_tasks: int | None = None,
    ) -> None:
        self.cfg = cfg if cfg is not None else Config()
        self.prompt_family = prompt_family or get_prompt_family(
            getattr(self.cfg, "prompt_family", "default"), self.cfg
        )
        self._llm_call = llm_call
        configured = int(getattr(self.cfg, "adaptive_max_tasks", DEFAULT_MAX_TASKS))
        self.max_tasks = int(max_tasks or configured or DEFAULT_MAX_TASKS)

    # ------------------------------------------------------------------ llm
    async def _complete(self, prompt: str) -> str:
        if self._llm_call is not None:
            return await self._llm_call(prompt)
        return await create_chat_completion(
            model=self.cfg.strategic_llm_model,
            messages=[{"role": "user", "content": prompt}],
            llm_provider=self.cfg.strategic_llm_provider,
            max_tokens=self.cfg.strategic_token_limit,
            llm_kwargs=self.cfg.llm_kwargs,
        )

    # ----------------------------------------------------------------- plan
    async def initial_plan(self, query: str) -> list[SubTask]:
        """Return 1..max_tasks sub-tasks for ``query`` (never empty)."""
        prompt = self.prompt_family.generate_plan_prompt(query, self.max_tasks)
        try:
            response = await self._complete(prompt)
        except Exception as exc:  # noqa: BLE001 - a bad LLM call must not kill the run
            logger.warning("adaptive planner LLM call failed: %s", exc)
            return self._fallback_plan(query)

        tasks = self.parse_plan(response, limit=self.max_tasks)
        if not tasks:
            logger.warning("adaptive planner produced no usable tasks; using fallback")
            return self._fallback_plan(query)
        return tasks

    def parse_plan(self, response: str, limit: int | None = None) -> list[SubTask]:
        """Parse an LLM plan response into tasks, tolerating fences and prose."""
        limit = limit or self.max_tasks
        payload = self._load_json(response)
        items = self._as_items(payload)

        tasks: list[SubTask] = []
        for item in items:
            query, goal = self._read_item(item)
            if not query:
                continue
            tasks.append(
                SubTask(
                    id=f"T{len(tasks) + 1}",
                    query=query,
                    goal=goal or query,
                    origin=TaskOrigin.INITIAL,
                )
            )
            if len(tasks) >= limit:
                break
        return tasks

    def _load_json(self, response: str) -> Any:
        candidates: list[str] = []
        text = (response or "").strip()
        if text:
            candidates.append(text)
        for match in _FENCE_RE.finditer(text):
            candidates.append(match.group("payload").strip())
        for pattern in (_ARRAY_RE, _OBJECT_RE):
            match = pattern.search(text)
            if match:
                candidates.append(match.group(0))
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return json_repair.loads(candidate)
            except Exception:  # noqa: BLE001 - try the next candidate
                continue
        return None

    @staticmethod
    def _as_items(payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("tasks", "plan", "sub_tasks", "subTasks", "queries", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
            # A single {"query": ...} object is still a plan of one task.
            if payload.get("query") or payload.get("task"):
                return [payload]
        if isinstance(payload, str) and payload.strip():
            return [payload]
        return []

    @staticmethod
    def _read_item(item: Any) -> tuple[str, str]:
        if isinstance(item, str):
            return item.strip(), ""
        if isinstance(item, dict):
            query = item.get("query") or item.get("task") or item.get("search_query") or ""
            goal = item.get("goal") or item.get("research_goal") or item.get("objective") or ""
            return str(query).strip(), str(goal).strip()
        return "", ""

    # --------------------------------------------------------------- planning
    async def revise(
        self,
        state,
        evaluation,
        max_new_tasks: int | None = None,
    ) -> list[SubTask]:
        """Turn evaluation gaps into new tasks (de-duplicated, bounded).

        Tasks with a ``suggested_query`` cost nothing; only gaps without one are
        sent to the LLM, and at most ``_MAX_GAP_LLM_CALLS`` times per revision.
        """
        deficits = [d for d in evaluation.deficits if d.kind in _GAP_KINDS]
        if not deficits:
            return []

        remaining = max(0, self.max_tasks - len(state.plan))
        limit = remaining if max_new_tasks is None else min(max_new_tasks, remaining)
        if limit <= 0:
            logger.info("adaptive planner: task budget exhausted, no new tasks")
            return []

        candidates: list[SubTask] = []
        llm_calls = 0
        for deficit in deficits:
            if len(candidates) >= limit * 2:
                break
            parent = state.task_by_id(deficit.task_id) if deficit.task_id else None
            if parent is not None and parent.status is TaskStatus.PENDING:
                # Already queued for the next round; no new task needed.
                continue
            query = (deficit.suggested_query or "").strip()
            if not query and llm_calls < self._MAX_GAP_LLM_CALLS:
                query = await self._gap_query(deficit)
                llm_calls += 1
            if not query:
                continue
            candidates.append(
                SubTask(
                    id="T?",
                    query=query,
                    goal=f"补齐缺口：{deficit.description[:120]}",
                    origin=TaskOrigin.REPLAN
                    if deficit.kind == "missing_coverage"
                    else TaskOrigin.GAP,
                    parent_id=deficit.task_id,
                )
            )

        # De-duplicate, but never against the task this gap came from: if the
        # evaluator says a done task was not actually covered, researching it
        # again is exactly the intent.
        kept: list[SubTask] = []
        for candidate in candidates:
            pool = [t for t in state.plan if t.id != candidate.parent_id]
            if self._find_duplicate(candidate, pool, DEDUPE_THRESHOLD) is not None:
                logger.debug("adaptive planner dropped duplicate gap task %r", candidate.query)
                continue
            kept.append(candidate)
        kept = kept[:limit]
        next_id = int(self.next_task_id(state.plan)[1:])
        for task in kept:
            task.id = f"T{next_id}"
            next_id += 1
        return kept

    async def _gap_query(self, deficit) -> str:
        """Ask the LLM for one search query that closes ``deficit``."""
        prompt = self.prompt_family.generate_gap_query_prompt(deficit.description)
        try:
            response = await self._complete(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("adaptive gap-query LLM call failed: %s", exc)
            return ""
        payload = self._load_json(response)
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, str) and item.strip():
                    return item.strip()
        if isinstance(payload, dict):
            value = payload.get("query") or payload.get("queries")
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, list) and value and isinstance(value[0], str):
                return value[0].strip()
        return ""

    def _fallback_plan(self, query: str) -> list[SubTask]:
        """Single-task plan used when the LLM answer is unusable.

        Deliberately minimal: a wrong-but-plausible synthetic plan is worse than
        one task that the evaluator can then flag as under-covering.
        """
        return [
            SubTask(
                id="T1",
                query=query,
                goal="完整回答原始研究问题，并给出可核查的来源",
                origin=TaskOrigin.INITIAL,
            )
        ]

    # ------------------------------------------------------------------ ids
    @staticmethod
    def next_task_id(existing: list[SubTask]) -> str:
        """Next free ``T<n>`` id; ids are never reused."""
        highest = 0
        for task in existing:
            match = _TASK_ID_RE.match(task.id)
            if match:
                highest = max(highest, int(match.group(1)))
        return f"T{highest + 1}"

    # --------------------------------------------------------------- dedupe
    @staticmethod
    def normalize_query(text: str) -> str:
        """Lower-case, strip punctuation and filler words, keep words/CJK."""
        text = (text or "").lower()
        text = re.sub(r"[^\w\s\u4e00-\u9fff]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        # Chinese questions differ mostly by filler ("有哪些", "的"); removing it
        # is what makes "RAG 系统的局限" and "RAG 系统有哪些局限" compare equal.
        for filler in _CJK_FILLERS:
            text = text.replace(filler, "")
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def query_similarity(cls, left: str, right: str) -> float:
        """0..100 similarity between two research queries."""
        a, b = cls.normalize_query(left), cls.normalize_query(right)
        if not a or not b:
            return 0.0
        if a == b:
            return 100.0
        return float(fuzz.token_set_ratio(a, b))

    def dedupe(
        self,
        candidates: list[SubTask],
        existing: list[SubTask],
        threshold: float = DEDUPE_THRESHOLD,
    ) -> list[SubTask]:
        """Drop candidate tasks that repeat an existing or already-kept task.

        A duplicate is not wasted information: its goal is merged into the kept
        task so the plan becomes slightly more specific instead of wider.
        """
        kept: list[SubTask] = []
        pool: list[SubTask] = list(existing)
        for candidate in candidates:
            match = self._find_duplicate(candidate, pool, threshold)
            if match is not None:
                self._merge_goal(match, candidate.goal)
                logger.debug(
                    "adaptive planner dropped duplicate task %r (similar to %s)",
                    candidate.query,
                    match.id,
                )
                continue
            pool.append(candidate)
            kept.append(candidate)
        return kept

    def _find_duplicate(
        self, candidate: SubTask, pool: list[SubTask], threshold: float
    ) -> SubTask | None:
        best: SubTask | None = None
        best_score = threshold
        for item in pool:
            score = self.query_similarity(candidate.query, item.query)
            if score >= best_score:
                best, best_score = item, score
        return best

    @staticmethod
    def _merge_goal(target: SubTask, extra: str) -> None:
        extra = (extra or "").strip()
        if not extra or extra in (target.goal or ""):
            return
        target.goal = f"{target.goal}；{extra}" if target.goal else extra
