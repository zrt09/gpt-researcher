"""T07/T08: planner parsing, fallback, and task de-duplication."""

import json

import pytest

from gpt_researcher.adaptive.planner import Planner
from gpt_researcher.adaptive.state import SubTask, TaskOrigin


@pytest.fixture
def planner():
    return Planner(llm_call=None)


def test_parse_plain_json_array(planner):
    response = json.dumps(
        [
            {"query": "rag limitations", "goal": "list three limitations"},
            {"query": "rag retrieval failures", "goal": "explain retrieval failures"},
        ]
    )
    tasks = planner.parse_plan(response)
    assert [t.id for t in tasks] == ["T1", "T2"]
    assert tasks[0].query == "rag limitations"
    assert tasks[0].goal == "list three limitations"
    assert tasks[0].origin is TaskOrigin.INITIAL


def test_parse_fenced_json(planner):
    response = 'Here is the plan:\n```json\n[{"query": "q1", "goal": "g1"}]\n```\nDone.'
    tasks = planner.parse_plan(response)
    assert len(tasks) == 1
    assert tasks[0].query == "q1"


def test_parse_object_with_tasks_key(planner):
    response = '{"tasks": [{"query": "a", "goal": "b"}]}'
    assert planner.parse_plan(response)[0].query == "a"


def test_parse_single_query_object(planner):
    response = '{"query": "only one", "goal": "why not"}'
    tasks = planner.parse_plan(response)
    assert len(tasks) == 1 and tasks[0].query == "only one"


def test_parse_list_of_strings(planner):
    tasks = planner.parse_plan('["first query", "second query"]')
    assert [t.query for t in tasks] == ["first query", "second query"]
    assert tasks[0].goal == "first query"  # goal falls back to the query


def test_parse_enforces_limit(planner):
    payload = [{"query": f"q{i}", "goal": "g"} for i in range(10)]
    assert len(planner.parse_plan(json.dumps(payload), limit=3)) == 3


def test_parse_skips_items_without_query(planner):
    tasks = planner.parse_plan('[{"goal": "no query"}, {"query": "ok", "goal": "g"}]')
    assert [t.query for t in tasks] == ["ok"]


def test_parse_garbage_returns_empty(planner):
    assert planner.parse_plan("this is not json at all") == []


async def test_initial_plan_uses_llm_response():
    async def fake_llm(prompt: str) -> str:
        assert "Original research question" in prompt
        return '[{"query": "q1", "goal": "g1"}, {"query": "q2", "goal": "g2"}]'

    planner = Planner(llm_call=fake_llm)
    tasks = await planner.initial_plan("what limits rag?")
    assert [t.query for t in tasks] == ["q1", "q2"]


async def test_initial_plan_falls_back_when_llm_returns_garbage():
    async def fake_llm(prompt: str) -> str:
        return "I cannot help with that."

    planner = Planner(llm_call=fake_llm)
    tasks = await planner.initial_plan("what limits rag?")
    assert len(tasks) == 1
    assert tasks[0].query == "what limits rag?"
    assert tasks[0].goal


async def test_initial_plan_falls_back_when_llm_raises():
    async def failing_llm(prompt: str) -> str:
        raise RuntimeError("provider down")

    planner = Planner(llm_call=failing_llm)
    tasks = await planner.initial_plan("q")
    assert len(tasks) == 1


def test_next_task_id_is_monotonic():
    existing = [SubTask(id="T2", query="a", goal="g"), SubTask(id="T7", query="b", goal="g")]
    assert Planner.next_task_id(existing) == "T8"
    assert Planner.next_task_id([]) == "T1"
