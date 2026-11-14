"""T14: executor adapter (evidence capture, failures, usage accounting)."""

import asyncio

import pytest

from gpt_researcher.adaptive.executor import ResearchExecutor
from gpt_researcher.adaptive.state import SubTask, TaskStatus


class FakeConductor:
    def __init__(self, error=None):
        self.last_error = error


class FakeResearcher:
    """Minimal stand-in with the same surface the executor uses."""

    def __init__(self, *, context="some context", pages=None, error=None, raise_exc=None,
                 delay=0.0, sources=None, **kwargs):
        self.context = context
        self.pages = pages if pages is not None else []
        self.research_conductor = FakeConductor(error)
        self.research_sources = sources or []
        self.raise_exc = raise_exc
        self.delay = delay
        self.kwargs = kwargs
        # Set by the executor after construction.
        self.artifact_sink = None

    async def conduct_research(self):
        from gpt_researcher.utils.llm import _ACTIVE_USAGE_SINK

        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_exc:
            raise self.raise_exc
        sink = _ACTIVE_USAGE_SINK.get()
        if sink:
            sink(input_tokens=10, output_tokens=5, cost_usd=0.001)
        if self.artifact_sink:
            self.artifact_sink(
                sub_query="q", pages=self.pages, compressed=self.context, stage="scrape"
            )
        return self.context


def _task(task_id="T1", query="q1"):
    return SubTask(id=task_id, query=query, goal="g")


def _factory(**init_kwargs):
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return FakeResearcher(**kwargs, **init_kwargs)

    factory.calls = calls
    return factory


async def test_executor_builds_evidence_from_pages():
    pages = [
        {"url": "https://a.com/x", "title": "A", "raw_content": "alpha text"},
        {"url": "https://b.com/y", "title": "B", "raw_content": "beta text"},
        {"url": "https://a.com/x", "title": "A dup", "raw_content": "alpha again"},
    ]
    executor = ResearchExecutor(researcher_factory=_factory(pages=pages))

    result = await executor.run_task(_task(), round_no=2)

    assert result.status is TaskStatus.DONE
    assert [e.url for e in result.evidence] == ["https://a.com/x", "https://b.com/y"]
    assert [e.id for e in result.evidence] == ["E1", "E2"]
    assert result.evidence[0].round == 2
    assert result.evidence[0].task_id == "T1"
    assert result.context == "some context"


async def test_evidence_urls_are_deduplicated_across_tasks():
    executor = ResearchExecutor(
        researcher_factory=_factory(pages=[{"url": "https://a.com/x", "raw_content": "a"}])
    )

    first = await executor.run_task(_task("T1"))
    second = await executor.run_task(_task("T2"))

    assert len(first.evidence) == 1
    assert second.evidence == []


async def test_executor_reports_failure_on_exception():
    executor = ResearchExecutor(researcher_factory=_factory(raise_exc=RuntimeError("search exploded")))

    result = await executor.run_task(_task())

    assert result.status is TaskStatus.FAILED
    assert "search exploded" in result.error


async def test_executor_reports_timeout():
    executor = ResearchExecutor(researcher_factory=_factory(delay=0.5), timeout_s=0.05)

    result = await executor.run_task(_task())

    assert result.status is TaskStatus.FAILED
    assert "timeout" in result.error


async def test_executor_fails_on_empty_context_without_evidence():
    executor = ResearchExecutor(researcher_factory=_factory(context=""))

    result = await executor.run_task(_task())

    assert result.status is TaskStatus.FAILED
    assert "empty context" in result.error


async def test_executor_keeps_partial_success_when_error_but_context_exists():
    executor = ResearchExecutor(
        researcher_factory=_factory(
            context="partial context", error=RuntimeError("one sub-query failed")
        )
    )

    result = await executor.run_task(_task())

    assert result.status is TaskStatus.DONE
    assert result.context == "partial context"


async def test_executor_records_usage_and_forwards_to_sink():
    seen = []
    executor = ResearchExecutor(
        researcher_factory=_factory(pages=[{"url": "https://a.com/x", "raw_content": "a"}]),
        usage_sink=lambda **kw: seen.append(kw),
    )

    result = await executor.run_task(_task())

    assert result.tokens == 15
    assert result.cost_usd == pytest.approx(0.001)
    assert len(seen) == 1 and seen[0]["input_tokens"] == 10


async def test_executor_passes_shared_visited_urls_to_researcher():
    shared = {"https://seen.com"}
    factory = _factory()
    executor = ResearchExecutor(
        researcher_factory=factory, shared_visited_urls=shared
    )

    await executor.run_task(_task())

    assert factory.calls[0]["visited_urls"] is shared
