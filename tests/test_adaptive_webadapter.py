"""B-plan: adaptive research behind the classic web UI."""

import asyncio
from types import SimpleNamespace

import pytest

from backend.report_type.adaptive_report.adaptive_report import AdaptiveReport


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, data):
        self.messages.append(data)


def make_result(report="# 报告\n\n正文", passed=True):
    evaluation = SimpleNamespace(
        passed=passed,
        metrics={
            "coverage": SimpleNamespace(value=0.8),
            "citation_match_url": SimpleNamespace(value=1.0),
            "completeness": SimpleNamespace(value=1.0),
            "source_validity": SimpleNamespace(value=0.9),
        },
    )
    budget = SimpleNamespace(rounds=1, cost_usd=0.06)
    return SimpleNamespace(
        report=report,
        evaluation=evaluation,
        status=SimpleNamespace(value="completed"),
        stop_reason=SimpleNamespace(value="quality_met"),
        state=SimpleNamespace(budget=budget),
    )


class FakeResearcher:
    def __init__(self, query, **kwargs):
        self.query = query
        self.kwargs = kwargs
        self.eval_mode = kwargs.get("eval_mode") or "light"
        self.state = SimpleNamespace(
            budget=SimpleNamespace(max_rounds=3, max_llm_calls=120)
        )
        self.on_round = None

    async def run(self, on_round=None):
        record = SimpleNamespace(
            round=1,
            new_evidence=12,
            evaluation=SimpleNamespace(
                metrics={
                    "coverage": SimpleNamespace(value=0.8),
                    "citation_match_url": SimpleNamespace(value=1.0),
                    "completeness": SimpleNamespace(value=1.0),
                    "source_validity": SimpleNamespace(value=0.9),
                }
            ),
            decision="finish",
        )
        on_round(record)
        await asyncio.sleep(0)  # let the scheduled log task run
        if self.kwargs.get("observer"):
            await self.kwargs["observer"]("adaptive_round_started", round=1)
        return make_result()


async def test_adapter_streams_logs_and_sends_report_once(monkeypatch):
    monkeypatch.setattr("gpt_researcher.adaptive.AdaptiveResearcher", FakeResearcher)
    websocket = FakeWebSocket()
    adapter = AdaptiveReport(query="2026年诺贝尔物理学奖", websocket=websocket)

    report = await adapter.run()
    await asyncio.sleep(0)

    logs = [m for m in websocket.messages if m.get("type") == "logs"]
    reports = [m for m in websocket.messages if m.get("type") == "report"]
    assert logs, "progress must be visible in the log panel"
    assert any("第 1 轮" in (m.get("output") or "") for m in logs)
    assert any("覆盖度" in (m.get("output") or "") for m in logs)
    assert len(reports) == 1
    assert reports[0]["output"] == report
    assert report.startswith("#")


async def test_adapter_survives_research_failure(monkeypatch):
    class Boom(FakeResearcher):
        async def run(self, on_round=None):
            raise RuntimeError("provider down")

    monkeypatch.setattr("gpt_researcher.adaptive.AdaptiveResearcher", Boom)
    websocket = FakeWebSocket()
    adapter = AdaptiveReport(query="q", websocket=websocket)

    report = await adapter.run()

    assert report.startswith("# 研究失败")
    assert any("provider down" in (m.get("output") or "") for m in websocket.messages)


async def test_adapter_survives_init_failure(monkeypatch):
    class BoomInit:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("bad config")

    monkeypatch.setattr("gpt_researcher.adaptive.AdaptiveResearcher", BoomInit)
    websocket = FakeWebSocket()
    adapter = AdaptiveReport(query="q", websocket=websocket)

    report = await adapter.run()

    assert report.startswith("# 研究失败")
    assert adapter.gpt_researcher is None


async def test_run_agent_routes_adaptive(monkeypatch):
    from backend.server import websocket_manager as wm

    captured = {}

    class FakeAdapter:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.gpt_researcher = None

        async def run(self):
            return "# adaptive report"

    class FakeHandler:
        def __init__(self, websocket, task):
            self.websocket = websocket
            self.task = task

        async def send_json(self, data):
            pass

    monkeypatch.setattr("backend.report_type.adaptive_report.AdaptiveReport", FakeAdapter)
    monkeypatch.setattr(wm, "CustomLogsHandler", FakeHandler)

    from gpt_researcher.utils.enum import Tone

    report = await wm.run_agent(
        "q", "adaptive", "web", [], [], Tone.Objective, FakeWebSocket(),
        adaptive_rounds=2, adaptive_eval_mode="full",
    )

    assert report == "# adaptive report"
    assert captured["report_type"] == "adaptive"
    assert captured["query"] == "q"
    assert captured["max_rounds"] == 2
    assert captured["eval_mode"] == "full"


def test_frontend_offers_adaptive_option():
    from pathlib import Path

    html = Path("frontend/index.html").read_text(encoding="utf-8")
    assert 'value="adaptive"' in html
    assert 'id="adaptiveRounds"' in html
    assert 'id="adaptiveEvalMode"' in html
    assert 'id="toneGroup"' in html
    assert 'id="reportSourceGroup"' in html
    assert 'id="adaptiveOptions"' in html


def test_frontend_script_sends_adaptive_controls():
    from pathlib import Path

    js = Path("frontend/scripts.js").read_text(encoding="utf-8")
    assert "syncAdaptiveFields" in js
    assert "adaptive_rounds" in js
    assert "adaptive_eval_mode" in js


async def test_adapter_forwards_domains_and_site_limit(monkeypatch):
    captured = {}

    class CaptureResearcher(FakeResearcher):
        def __init__(self, query, **kwargs):
            captured.update(kwargs)
            super().__init__(query, **kwargs)

    monkeypatch.setattr("gpt_researcher.adaptive.AdaptiveResearcher", CaptureResearcher)
    adapter = AdaptiveReport(
        query="q",
        websocket=FakeWebSocket(),
        query_domains=["nobelprize.org"],
        max_search_results=3,
        max_rounds=2,
        eval_mode="light",
    )

    await adapter.run()
    await asyncio.sleep(0)

    assert captured["query_domains"] == ["nobelprize.org"]
    assert captured["max_search_results"] == 3
    assert captured["max_rounds"] == 2
    assert captured["eval_mode"] == "light"


def test_adapter_clamps_browser_values():
    adapter = AdaptiveReport(
        query="q",
        max_rounds=99,
        eval_mode="bogus",
        max_search_results=0,
    )
    assert adapter.max_rounds == 5
    assert adapter.eval_mode is None
    assert adapter.max_search_results == 1

    adapter2 = AdaptiveReport(query="q", max_rounds=0, max_search_results="abc")
    assert adapter2.max_rounds == 1
    assert adapter2.max_search_results is None


def test_extract_command_data_includes_adaptive_fields():
    from backend.server.server_utils import extract_command_data

    fields = extract_command_data(
        {
            "task": "q",
            "report_type": "adaptive",
            "adaptive_rounds": 2,
            "adaptive_eval_mode": "full",
        }
    )

    assert len(fields) == 14
    assert fields[-2] == 2
    assert fields[-1] == "full"


def test_executor_applies_domains_and_site_limit(monkeypatch):
    from gpt_researcher.adaptive.executor import ResearchExecutor
    from gpt_researcher.adaptive.state import SubTask

    captured = {}

    class FakeCfg:
        max_search_results_per_query = 5

    class FakeGPT:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.cfg = FakeCfg()

    monkeypatch.setattr("gpt_researcher.agent.GPTResearcher", FakeGPT)

    executor = ResearchExecutor(
        query_domains=["nature.com", "science.org"], max_search_results=2
    )
    task = SubTask(id="T1", query="q", goal="g")
    researcher = executor._default_factory(task=task, config_path=None)

    assert captured["query_domains"] == ["nature.com", "science.org"]
    assert researcher.cfg.max_search_results_per_query == 2
