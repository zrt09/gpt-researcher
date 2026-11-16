"""T: outline-first structured writer (offline, fake LLM)."""

import json

import pytest

from gpt_researcher.adaptive.state import Evidence, ResearchState, SubTask
from gpt_researcher.adaptive.writer_v2 import StructuredWriter

OUTLINE = {
    "title": "2026 Nobel Prize in Physics",
    "sections": [
        {"title": "The Laureate", "purpose": "who won and why"},
        {"title": "The Discovery", "purpose": "what was discovered"},
        {"title": "Significance", "purpose": "why it matters"},
        {"title": "Open Questions", "purpose": "what remains unclear"},
    ],
}


def make_state(query="2026 Nobel Prize in Physics?") -> ResearchState:
    evidence = [
        Evidence(
            id=f"E{i}",
            task_id="T1",
            url=f"https://e.com/{i}",
            title=f"Source {i}",
            snippet="The laureate discovered high energy neutrinos with IceCube.",
            query="q",
            round=1,
            fetched_at="now",
            published_at="2026-10-06",
        )
        for i in range(1, 4)
    ]
    return ResearchState(
        run_id="r",
        query=query,
        plan=[SubTask(id="T1", query="q", goal="g")],
        evidence=evidence,
    )


async def fake_llm(prompt: str) -> str:
    lowered = prompt.lower()
    if "designing the structure" in lowered:
        return json.dumps(OUTLINE)
    if "write one section" in lowered:
        return "Neutrinos were detected. [Source 1](https://e.com/1)"
    if "executive summary" in lowered:
        return "The 2026 prize went to the IceCube team. [Source 1](https://e.com/1)"
    if "write the conclusion" in lowered:
        return "The discovery opened a new window on the universe."
    return "unexpected"


async def test_structured_writer_builds_clean_outline():
    writer = StructuredWriter(llm_call=fake_llm)
    report = await writer(make_state(), [])

    assert report.startswith("# 2026 Nobel Prize in Physics")
    assert "## Executive Summary" in report
    assert "## 1. The Laureate" in report
    assert "## 2. The Discovery" in report
    assert "## 4. Open Questions" in report
    assert "## Conclusion" in report
    assert "## References" in report
    # deterministic references come from evidence, deduplicated
    assert report.count("https://e.com/1") >= 2
    assert "- [Source 1](https://e.com/1)" in report


async def test_structured_writer_never_repeats_section_heading():
    async def llm(prompt: str) -> str:
        if "designing the structure" in prompt.lower():
            return json.dumps({"title": "T", "sections": OUTLINE["sections"]})
        if "write one section" in prompt.lower():
            return "## The Laureate\n\nBody with [s](https://e.com/1)"
        return "short"

    writer = StructuredWriter(llm_call=llm)
    report = await writer(make_state(), [])
    assert report.count("The Laureate") == 1


async def test_structured_writer_falls_back_to_default_outline():
    async def llm(prompt: str) -> str:
        if "designing the structure" in prompt.lower():
            return "not json at all"
        return "Body text with [s](https://e.com/1)"

    writer = StructuredWriter(llm_call=llm)
    report = await writer(make_state(), [])

    assert "## 1. Background" in report
    assert "## 5. Open Questions" in report


async def test_structured_writer_writes_chinese_for_chinese_query():
    async def llm(prompt: str) -> str:
        if "设计" in prompt or "designing the structure" in prompt.lower():
            return json.dumps(
                {
                    "title": "2026 年诺贝尔物理学奖",
                    "sections": [
                        {"title": "获奖者", "purpose": "谁获奖"},
                        {"title": "核心发现", "purpose": "发现了什么"},
                        {"title": "意义", "purpose": "为什么重要"},
                        {"title": "未解问题", "purpose": "还有什么不清楚"},
                    ],
                }
            )
        if "写" in prompt or "write one section" in prompt.lower():
            return "他发现中微子 [来源](https://e.com/1)。"
        return "摘要内容。"

    writer = StructuredWriter(llm_call=llm)
    report = await writer(make_state("2026年诺贝尔物理学奖得主及其主要贡献"), [])

    assert "## 摘要" in report
    assert "## 1. 获奖者" in report
    assert "## 参考文献" in report


async def test_structured_writer_returns_empty_without_evidence():
    writer = StructuredWriter(llm_call=fake_llm)
    empty = ResearchState(run_id="r", query="q")
    assert await writer(empty, []) == ""
