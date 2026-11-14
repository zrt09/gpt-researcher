"""T15: ResearchConductor records failures instead of silently returning empty."""

from types import SimpleNamespace

import pytest

from gpt_researcher.skills.researcher import ResearchConductor


class DummyRetriever:
    pass


def make_conductor(**overrides):
    researcher = SimpleNamespace(
        query="q",
        verbose=False,
        websocket=None,
        agent="researcher",
        role="role prompt",
        retrievers=[DummyRetriever],
        source_urls=None,
        report_source="web",
        query_domains=[],
        cfg=SimpleNamespace(curate_sources=False),
        research_observer=None,
        artifact_sink=None,
        context="",
    )
    for key, value in overrides.items():
        setattr(researcher, key, value)
    return ResearchConductor(researcher)


async def test_initial_search_failure_propagates(monkeypatch):
    """A failure before planning must surface, not be swallowed as empty context."""
    conductor = make_conductor()

    async def boom(*args, **kwargs):
        raise RuntimeError("retriever down")

    monkeypatch.setattr(conductor, "_get_initial_search_results", boom)

    with pytest.raises(RuntimeError, match="retriever down"):
        await conductor._get_context_by_web_search("q")


async def test_sub_query_failure_is_recorded(monkeypatch):
    conductor = make_conductor()

    async def boom(*args, **kwargs):
        raise RuntimeError("scrape failed")

    monkeypatch.setattr(conductor, "_scrape_data_by_urls", boom)

    result = await conductor._process_sub_query("q")

    assert result == ""
    assert isinstance(conductor.last_error, RuntimeError)


async def test_conduct_research_resets_previous_error(monkeypatch):
    conductor = make_conductor()
    conductor.last_error = RuntimeError("stale")

    async def fake_web_search(*args, **kwargs):
        return "fresh context"

    monkeypatch.setattr(conductor, "_get_context_by_web_search", fake_web_search)

    context = await conductor.conduct_research()

    assert context == "fresh context"
    assert conductor.last_error is None


async def test_artifact_sink_failure_does_not_break_research():
    captured = []

    def broken_sink(**kwargs):
        captured.append(kwargs)
        raise RuntimeError("sink exploded")

    conductor = make_conductor(artifact_sink=broken_sink)

    # Should not raise even though the sink always fails.
    await conductor._emit_artifact("q", [{"url": "https://a.com"}], "ctx", "scrape")
    assert captured
