"""Adaptive research adapter for the classic GPT Researcher web UI.

The legacy page talks to ``/ws`` with ``start {..report_type..}`` and renders
three message kinds: ``logs`` (shown in the activity panel), ``report`` (the
markdown body) and ``path`` (download links, produced by the caller).

This adapter makes ``report_type="adaptive"`` a first-class option:

* rounds, decisions and quality metrics are streamed as ordinary ``logs``
  messages, so the existing UI shows progress without any JS change;
* the finished report is sent once as a ``report`` message (the structured
  writer produces sections in parallel, so token streaming would interleave);
* the returned string is the same markdown, which is what the caller turns into
  md/docx/pdf/json downloads.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gpt_researcher.actions import stream_output
from gpt_researcher.utils.enum import Tone

logger = logging.getLogger(__name__)

METRIC_LABELS = (
    ("coverage", "覆盖度"),
    ("citation_match_url", "引用可回溯"),
    ("completeness", "完整性"),
    ("source_validity", "来源有效性"),
)
STATUS_LABELS = {
    "completed": "已完成（质量达标）",
    "partial": "部分完成（存在未解决问题）",
    "failed": "失败",
}
DECISION_LABELS = {
    "supplement": "补充研究",
    "replan": "重新规划",
    "rewrite": "重写报告",
    "finish": "结束",
    "abort": "中止",
}


def _clamp_int(value, low: int, high: int) -> int | None:
    """Clamp a value coming from the browser; ``None``/garbage means "use config"."""
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(low, min(high, number))


class AdaptiveReport:
    """Runs :class:`AdaptiveResearcher` and streams its progress to the UI."""

    def __init__(
        self,
        query: str,
        report_type: str = "adaptive",
        report_source: str = "web",
        tone: Tone = Tone.Objective,
        config_path: str | None = None,
        websocket: Any | None = None,
        headers: dict | None = None,
        query_domains: list | None = None,
        source_urls: list | None = None,
        document_urls: list | None = None,
        mcp_configs=None,
        mcp_strategy=None,
        max_search_results=None,
        max_rounds: int | None = None,
        eval_mode: str | None = None,
    ) -> None:
        self.query = query
        self.report_type = report_type
        self.report_source = report_source
        self.tone = tone
        self.config_path = config_path
        self.websocket = websocket
        self.headers = headers or {}
        self.query_domains = query_domains or []
        self.source_urls = source_urls or []
        self.document_urls = document_urls or []
        self.max_rounds = _clamp_int(max_rounds, 1, 5)
        self.eval_mode = eval_mode if eval_mode in ("light", "full") else None
        self.max_search_results = _clamp_int(max_search_results, 1, 20)
        # ``run_agent(return_researcher=True)`` expects this attribute.
        self.gpt_researcher = None

    # ------------------------------------------------------------------ output
    async def _log(self, step: str, text: str, metadata=None) -> None:
        try:
            await stream_output("logs", step, text, self.websocket, metadata=metadata)
        except Exception as exc:  # noqa: BLE001 - logging must never break research
            logger.debug("adaptive web log failed: %s", exc)

    async def _observe(self, event: str, **fields: Any) -> None:
        """Map adaptive lifecycle events onto human-readable log lines."""
        if event == "adaptive_round_started":
            round_no = fields.get("round", "?")
            await self._log("starting_research", f"🔄 第 {round_no} 轮研究开始")
        elif event == "reflection_decided":
            action = DECISION_LABELS.get(str(fields.get("action")), fields.get("action"))
            reason = fields.get("reason", "")
            await self._log("research_plan", f"🧭 评估结论：{action}（{reason}）")
        elif event == "query_planned":
            position = fields.get("position", "?")
            query = fields.get("query", "")
            await self._log("subqueries", f"🔎 子任务 {position}：{query}")
        else:
            logger.debug("adaptive event %s: %s", event, fields)

    def _on_round(self, record) -> None:
        """Called synchronously by the orchestrator after each round."""
        try:
            asyncio.get_running_loop().create_task(self._log_round(record))
        except RuntimeError:  # pragma: no cover - defensive
            logger.debug("no running loop for round log")

    async def _log_round(self, record) -> None:
        line = f"✅ 第 {record.round} 轮完成：新增证据 {record.new_evidence} 条"
        evaluation = record.evaluation
        if evaluation is not None:
            line += "；" + self._format_metrics(evaluation)
        if record.decision:
            line += f"；决策：{DECISION_LABELS.get(record.decision, record.decision)}"
        await self._log("research_step_finalized", line)

    @staticmethod
    def _format_metrics(evaluation) -> str:
        parts = []
        for name, label in METRIC_LABELS:
            metric = evaluation.metrics.get(name)
            value = None if metric is None else metric.value
            parts.append(f"{label} {'—' if value is None else f'{value:.2f}'}")
        return " / ".join(parts)

    async def _send_report(self, report: str) -> None:
        if not report:
            return
        try:
            await self.websocket.send_json({"type": "report", "output": report})
        except Exception as exc:  # noqa: BLE001
            logger.warning("adaptive report send failed: %s", exc)

    # --------------------------------------------------------------------- run
    async def run(self) -> str:
        from gpt_researcher.adaptive import AdaptiveResearcher

        try:
            researcher = AdaptiveResearcher(
                self.query,
                config_path=self.config_path,
                websocket=self.websocket,
                observer=self._observe,
                max_rounds=self.max_rounds,
                eval_mode=self.eval_mode,
                query_domains=self.query_domains,
                max_search_results=self.max_search_results,
            )
        except Exception as exc:  # noqa: BLE001 - surface config errors in the UI
            logger.exception("adaptive researcher init failed")
            await self._log("error", f"❌ 自适应研究初始化失败：{exc}")
            return f"# 研究失败\n\n自适应研究初始化失败：{exc}\n"

        budget = researcher.state.budget
        domains = "、".join(self.query_domains) if self.query_domains else "不限"
        sites = self.max_search_results or "默认"
        await self._log(
            "starting_research",
            f"🚀 自适应模式：最多 {budget.max_rounds} 轮、{budget.max_llm_calls} 次模型调用；"
            f"每查询抓取 {sites} 个站点；域名 {domains}；评估模式 {researcher.eval_mode}",
        )

        try:
            result = await researcher.run(on_round=self._on_round)
        except Exception as exc:  # noqa: BLE001 - never leave the UI hanging
            logger.exception("adaptive research failed")
            await self._log("error", f"❌ 自适应研究失败：{exc}")
            return f"# 研究失败\n\n本次自适应研究未能完成：{exc}\n"

        status = STATUS_LABELS.get(result.status.value, result.status.value)
        reason = result.stop_reason.value if result.stop_reason else "unknown"
        await self._log(
            "research_step_finalized",
            f"🏁 研究结束：{status}（stop_reason={reason}，"
            f"{result.state.budget.rounds} 轮，成本估算 ${result.state.budget.cost_usd:.4f}）",
        )
        await self._send_report(result.report)
        return result.report
