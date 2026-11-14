"""Adaptive planner: turn one research question into verifiable sub-tasks.

The planner is deliberately thin: it asks the strategic LLM for a small JSON
plan, validates it defensively, and falls back to a single research task rather
than failing the whole run. All network/LLM access goes through an injectable
``llm_call`` so unit tests run offline.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

import json_repair

from ..config import Config
from ..prompts import PromptFamily, get_prompt_family
from ..utils.llm import create_chat_completion
from .state import SubTask, TaskOrigin

logger = logging.getLogger(__name__)

DEFAULT_MAX_TASKS = 6
_FENCE_RE = re.compile(r"```(?:json)?\s*(?P<payload>[\s\S]*?)```", re.IGNORECASE)
_ARRAY_RE = re.compile(r"\[[\s\S]*\]")
_OBJECT_RE = re.compile(r"\{[\s\S]*\}")
_TASK_ID_RE = re.compile(r"^T(\d+)$")

LLMCall = Callable[[str], Awaitable[str]]


class Planner:
    """Builds the initial plan; revision lives in a later batch."""

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
