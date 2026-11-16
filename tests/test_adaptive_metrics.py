"""T10/T11: zero-cost metrics and URL validity sampling."""

from datetime import datetime, timedelta, timezone

from gpt_researcher.adaptive.metrics import (
    check_urls,
    compute_citation_match,
    compute_completeness,
    compute_coverage,
    compute_freshness,
    normalize_url,
)
from gpt_researcher.adaptive.state import (
    Claim,
    Evidence,
    ResearchState,
    SubTask,
    TaskStatus,
)

REPORT = """# Title

## Introduction

This report studies retrieval systems and their behaviour under load. https://a.com/x

## Findings one

The first finding says retrieval recall dominates quality in most deployments today, because a missing document cannot be recovered by any amount of generation. https://a.com/x

## Findings two

The second finding says latency grows with corpus size and reranking depth, which makes the retrieval budget the dominant cost at scale. https://b.com/y

## Conclusion

Retrieval and generation must be evaluated together. https://a.com/x

## References

- https://a.com/x
"""


def _task(task_id="T1", status=TaskStatus.DONE, query="q", goal="g"):
    return SubTask(id=task_id, query=query, goal=goal, status=status)


def _evidence(ev_id="E1", url="https://a.com/x", published="2025-06-01"):
    return Evidence(
        id=ev_id,
        task_id="T1",
        url=url,
        title="t",
        snippet="s",
        query="q",
        round=1,
        fetched_at="2026-11-14T00:00:00Z",
        published_at=published,
    )


def test_normalize_url_ignores_case_fragment_and_trailing_slash():
    assert normalize_url("HTTPS://Example.COM/a/#frag") == normalize_url("https://example.com/a")
    assert normalize_url("https://example.com/a/") == "https://example.com/a"


def test_coverage_counts_done_and_attributed_tasks():
    state = ResearchState(run_id="r", query="q", plan=[_task("T1"), _task("T2", TaskStatus.DONE)])
    claims = [Claim(id="C1", text="t", section="s", task_ids=["T1"])]
    metric = compute_coverage(state, claims)
    assert metric.value == 0.5
    assert metric.numerator == 1 and metric.denominator == 2


def test_coverage_none_without_tasks():
    state = ResearchState(run_id="r", query="q", plan=[])
    assert compute_coverage(state, []).value is None


def test_coverage_accepts_a_cited_source_from_the_task():
    """A section can answer a task without reusing its wording."""
    state = ResearchState(
        run_id="r",
        query="q",
        plan=[_task("T1"), _task("T2")],
        evidence=[_evidence("E1", url="https://a.com/x")],
    )
    claims = [Claim(id="C1", text="t", section="s", cited_urls=["https://a.com/x"])]

    metric = compute_coverage(state, claims)

    assert metric.value == 0.5
    assert "T1" not in metric.notes


def test_coverage_matches_chinese_tasks_by_distinctive_terms():
    tasks = [
        SubTask(id="T1", query="2026年诺贝尔物理学奖获奖者", goal="答案必须列出获奖者姓名", status=TaskStatus.DONE),
        SubTask(id="T2", query="该奖成果的科学意义", goal="答案必须说明科学意义", status=TaskStatus.DONE),
        SubTask(id="T3", query="官方公告与颁奖信息", goal="答案必须给出官方公告时间", status=TaskStatus.DONE),
        SubTask(id="T4", query="学界与媒体反响", goal="答案必须提供学界与媒体反响", status=TaskStatus.DONE),
        SubTask(id="T5", query="冰立方中微子天文台", goal="答案必须介绍冰立方项目", status=TaskStatus.DONE),
    ]
    sections = [
        "获奖者与官方信息",
        "颁奖公告与奖项定位",
        "冰立方中微子天文台项目",
        "学界与媒体反响",
        "该成果的科学意义与影响",
    ]
    claims = [
        Claim(id=f"C{i}", text=f"{title}的具体内容说明。", section=title)
        for i, title in enumerate(sections, start=1)
    ]
    state = ResearchState(run_id="r", query="2026年诺贝尔物理学奖", plan=tasks)

    metric = compute_coverage(state, claims)

    assert metric.value is not None
    assert metric.value >= 0.8, metric.notes


def test_citation_match_uses_normalized_urls():
    claims = [
        Claim(id="C1", text="t", section="s", cited_urls=["https://a.com/x/"]),
        Claim(id="C2", text="t", section="s", cited_urls=["https://unknown.com/z"]),
    ]
    metric = compute_citation_match(claims, [_evidence(url="https://a.com/x")])
    assert metric.value == 0.5
    assert metric.numerator == 1 and metric.denominator == 2


def test_citation_match_none_without_citations():
    assert compute_citation_match([], []).value is None


def test_completeness_full_report():
    metric = compute_completeness(REPORT)
    assert metric.value == 1.0
    assert metric.denominator == 4


def test_completeness_reports_missing_parts():
    report = "# Title\n\n## Introduction\n\n" + "word " * 40 + " https://a.com/x\n"
    metric = compute_completeness(report)
    assert metric.value is not None and metric.value < 1.0
    assert "body" in metric.notes or "conclusion" in metric.notes


def test_completeness_accepts_executive_summary_as_intro():
    report = (
        "# Title\n\n"
        "## Executive Summary\n\nShort summary of the findings. https://a.com/x\n\n"
        "## Findings one\n\n" + "word " * 40 + " https://a.com/x\n\n"
        "## Findings two\n\n" + "word " * 40 + " https://b.com/y\n\n"
        "## Conclusion\n\nDone. https://a.com/x\n\n"
        "## References\n\n- https://a.com/x\n"
    )
    metric = compute_completeness(report)
    assert metric.value == 1.0
    assert "intro" not in metric.notes


def test_completeness_accepts_chinese_sections():
    report = (
        "# 标题\n\n"
        "## 摘要\n\n这是摘要。https://a.com/x\n\n"
        "## 研究发现一\n\n" + "内容 " * 60 + " https://a.com/x\n\n"
        "## 研究发现二\n\n" + "内容 " * 60 + " https://b.com/y\n\n"
        "## 结论\n\n结束。https://a.com/x\n\n"
        "## 参考文献\n\n- https://a.com/x\n"
    )
    metric = compute_completeness(report)
    assert metric.value == 1.0


def test_freshness_only_counts_dated_evidence():
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    old = (now - timedelta(days=365 * 4)).date().isoformat()
    evidence = [_evidence("E1", published="2025-06-01"), _evidence("E2", published=old)]
    metric = compute_freshness(evidence, months=24, now=now)
    assert metric.value == 0.5
    assert metric.denominator == 2


def test_freshness_none_when_no_dates():
    metric = compute_freshness([_evidence(published=None)], months=24)
    assert metric.value is None


async def test_check_urls_excludes_blocked_from_score():
    async def sampler(url: str) -> str:
        return {
            "https://a.com/ok": "ok",
            "https://a.com/gone": "invalid",
            "https://a.com/wall": "blocked",
        }[url]

    metric = await check_urls(
        ["https://a.com/ok", "https://a.com/gone", "https://a.com/wall"], sampler=sampler
    )
    assert metric.value == 0.5  # ok / (ok + invalid)
    assert "blocked=1" in metric.notes


async def test_check_urls_handles_sampler_exception():
    async def sampler(url: str) -> str:
        raise RuntimeError("boom")

    metric = await check_urls(["https://a.com/x"], sampler=sampler)
    assert metric.value is None
    assert "errors=1" in metric.notes


async def test_check_urls_is_deterministic_and_samples():
    urls = [f"https://a.com/{i}" for i in range(50)]
    seen = []

    async def sampler(url: str) -> str:
        seen.append(url)
        return "ok"

    metric = await check_urls(urls, sampler=sampler, sample=5)
    assert len(seen) == 5
    assert metric.sampled is True
    assert metric.value == 1.0


async def test_check_urls_none_without_urls():
    metric = await check_urls([])
    assert metric.value is None
