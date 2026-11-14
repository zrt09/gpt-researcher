"""T04: create_chat_completion reports usage without changing old behaviour."""

import pytest

from gpt_researcher.utils import llm as llm_module


class FakeProvider:
    def __init__(self, response="hello", usage=None):
        self._response = response
        self.last_usage_metadata = usage
        self.last_response_metadata = {}

    async def get_chat_response(self, messages, stream, websocket=None, **kwargs):
        return self._response


@pytest.fixture
def patch_provider(monkeypatch):
    def _patch(provider):
        monkeypatch.setattr(llm_module, "get_llm", lambda *a, **kw: provider)
        return provider

    return _patch


async def test_no_sink_keeps_previous_behaviour(patch_provider):
    patch_provider(FakeProvider("answer", {"input_tokens": 10, "output_tokens": 5}))
    result = await llm_module.create_chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        model="test-model",
        llm_provider="openai",
    )
    assert result == "answer"


async def test_sink_receives_reported_usage(patch_provider):
    patch_provider(FakeProvider("answer", {"input_tokens": 123, "output_tokens": 45}))
    seen = []
    result = await llm_module.create_chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        model="test-model",
        llm_provider="openai",
        usage_sink=lambda **kw: seen.append(kw),
    )
    assert result == "answer"
    assert len(seen) == 1
    usage = seen[0]
    assert usage["input_tokens"] == 123
    assert usage["output_tokens"] == 45
    assert usage["estimated"] is False
    assert usage["model"] == "test-model"
    assert usage["provider"] == "openai"
    assert usage["cost_usd"] >= 0.0


async def test_sink_is_told_when_usage_was_estimated(patch_provider):
    patch_provider(FakeProvider("x" * 400, None))
    seen = []
    await llm_module.create_chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        model="test-model",
        llm_provider="openai",
        usage_sink=lambda **kw: seen.append(kw),
    )
    assert seen[0]["estimated"] is True
    assert seen[0]["input_tokens"] > 0
    assert seen[0]["output_tokens"] > 0


async def test_failing_sink_does_not_break_the_call(patch_provider):
    patch_provider(FakeProvider("still works", {"input_tokens": 1, "output_tokens": 1}))

    def broken_sink(**kwargs):
        raise RuntimeError("sink exploded")

    result = await llm_module.create_chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        model="test-model",
        llm_provider="openai",
        usage_sink=broken_sink,
    )
    assert result == "still works"
