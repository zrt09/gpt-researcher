"""Built-in adaptive web UI (HTTP surface, no real research)."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.server import adaptive_ui


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(adaptive_ui.router)
    return TestClient(app)


def test_page_renders(client):
    response = client.get("/adaptive-ui")
    assert response.status_code == 200
    assert "Adaptive Research Agent" in response.text
    assert "adaptive-ui/api/run" in response.text


def test_status_unknown_run_is_404(client):
    assert client.get("/adaptive-ui/api/status/missing").status_code == 404


def test_report_unknown_run_is_404(client):
    assert client.get("/adaptive-ui/api/report/missing").status_code == 404


def test_run_rejects_empty_query(client):
    assert client.post("/adaptive-ui/api/run", json={"query": "   "}).status_code == 400


async def test_execute_marks_run_completed(monkeypatch):
    class FakeEvaluation:
        metrics = {
            "coverage": SimpleNamespace(value=1.0, notes="", numerator=2, denominator=2)
        }
        claims = [1, 2, 3]
        deficits = []
        passed = True

    class FakeResearcher:
        def __init__(self, query, **kwargs):
            self.state = SimpleNamespace(
                budget=SimpleNamespace(max_rounds=2), plan=[], evidence=[]
            )

        async def run(self, on_round=None):
            on_round(SimpleNamespace(round=1, evaluation=FakeEvaluation(), decision="finish",
                                     new_evidence=3))
            return SimpleNamespace(
                status=SimpleNamespace(value="completed"),
                stop_reason=SimpleNamespace(value="quality_met"),
                evaluation=FakeEvaluation(),
                report="# done",
                directory=None,
            )

    monkeypatch.setattr("gpt_researcher.adaptive.AdaptiveResearcher", FakeResearcher)

    run_id = "testrun1"
    adaptive_ui._runs[run_id] = {
        "id": run_id,
        "query": "q",
        "status": "running",
        "round": 0,
        "max_rounds": 0,
        "metrics": {},
        "log": [],
    }
    await adaptive_ui._execute(run_id, adaptive_ui.RunRequest(query="q"))

    entry = adaptive_ui._runs[run_id]
    assert entry["status"] == "completed"
    assert entry["report"] == "# done"
    assert entry["metrics"]["coverage"]["value"] == 1.0
    assert entry["round"] == 1


async def test_execute_records_failure(monkeypatch):
    class Boom:
        def __init__(self, query, **kwargs):
            raise RuntimeError("provider down")

    monkeypatch.setattr("gpt_researcher.adaptive.AdaptiveResearcher", Boom)

    run_id = "testrun2"
    adaptive_ui._runs[run_id] = {
        "id": run_id,
        "query": "q",
        "status": "running",
        "round": 0,
        "max_rounds": 0,
        "metrics": {},
        "log": [],
    }
    await adaptive_ui._execute(run_id, adaptive_ui.RunRequest(query="q"))

    entry = adaptive_ui._runs[run_id]
    assert entry["status"] == "failed"
    assert "provider down" in entry["error"]
