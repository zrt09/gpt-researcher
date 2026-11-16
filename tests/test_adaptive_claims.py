"""T09: rule-based claim extraction."""

from gpt_researcher.adaptive.claims import (
    extract_claims,
    extract_urls,
    split_sections,
)
from gpt_researcher.adaptive.state import SubTask, TaskStatus

REPORT = """# RAG report

## Introduction

Retrieval-augmented generation combines a retriever with a generator, and its quality depends on both parts. https://example.com/intro

## RAG limitations

Retrieval failures dominate the error budget in production RAG deployments. https://example.com/a
Hallucination still occurs when the retrieved context is irrelevant to the question. https://example.com/b

## Conclusion

RAG reduces but does not eliminate hallucination, so evaluation must stay in the loop. https://example.com/c

## References

- https://example.com/a
- https://example.com/b
"""


def test_split_sections_keeps_urls_and_titles():
    sections = split_sections(REPORT)
    titles = [s.title for s in sections]
    assert "RAG limitations" in titles
    body = next(s.body for s in sections if s.title == "RAG limitations")
    assert "https://example.com/a" in body


def test_extract_urls_dedupes_and_trims_punctuation():
    urls = extract_urls("See https://a.com/x, and https://a.com/x. Also https://b.com/y!")
    assert urls == ["https://a.com/x", "https://b.com/y"]


def test_extract_urls_stops_at_chinese_punctuation():
    text = "获奖信息来自 https://www.nobelprize.org/all-nobel-prizes-2026，另见 https://example.com/zh。"
    assert extract_urls(text) == [
        "https://www.nobelprize.org/all-nobel-prizes-2026",
        "https://example.com/zh",
    ]


def test_extract_claims_skips_references():
    claims = extract_claims(REPORT)
    assert claims
    assert all("References" not in c.section for c in claims)


def test_claim_ids_are_sequential():
    claims = extract_claims(REPORT)
    assert [c.id for c in claims] == [f"C{i + 1}" for i in range(len(claims))]


def test_claims_capture_cited_urls():
    claims = extract_claims(REPORT)
    cited = {url for c in claims for url in c.cited_urls}
    assert "https://example.com/a" in cited


def test_claim_attributes_to_matching_task():
    tasks = [
        SubTask(id="T1", query="RAG limitations", goal="list RAG limitations"),
        SubTask(id="T2", query="vector database pricing", goal="compare prices"),
    ]
    claims = extract_claims(REPORT, tasks)
    limitation_claims = [c for c in claims if c.section == "RAG limitations"]
    assert limitation_claims
    assert any("T1" in c.task_ids for c in limitation_claims)
    assert all("T2" not in c.task_ids for c in limitation_claims)


def test_chinese_claims_attribute_to_chinese_tasks():
    report = (
        "## 获奖者与主要贡献\n\n"
        "2026年诺贝尔物理学奖授予弗朗西斯·哈尔岑，表彰其在冰立方中微子天文台的开创性工作。"
        " https://e.com/1\n"
    )
    tasks = [
        SubTask(id="T1", query="2026诺贝尔物理学奖获奖者", goal="说明获奖者及其主要贡献"),
        SubTask(id="T2", query="历年诺贝尔物理学奖趋势", goal="比较历年趋势"),
    ]
    claims = extract_claims(report, tasks)
    assert claims
    assert "T1" in claims[0].task_ids
    assert "T2" not in claims[0].task_ids


def test_skipped_tasks_are_not_attributed():
    tasks = [
        SubTask(
            id="T1",
            query="RAG limitations",
            goal="list RAG limitations",
            status=TaskStatus.SKIPPED,
        )
    ]
    claims = extract_claims(REPORT, tasks)
    assert all(not c.task_ids for c in claims)


def test_max_claims_is_enforced():
    long_report = "## Body\n\n" + "\n".join(
        f"Claim number {i} about retrieval systems with enough words to count. https://e.com/{i}"
        for i in range(50)
    )
    claims = extract_claims(long_report, max_claims=5)
    assert len(claims) == 5


def test_short_sentences_are_ignored():
    claims = extract_claims("## X\n\nToo short.\n")
    assert claims == []


def test_markdown_links_keep_their_url():
    report = (
        "## Findings\n\n"
        "Retrieval failures dominate the error budget in production deployments "
        "([Barnett, 2024](https://arxiv.org/html/2401.05856v1)).\n"
    )
    claims = extract_claims(report)
    assert claims
    assert claims[0].cited_urls == ["https://arxiv.org/html/2401.05856v1"]
    assert "https://arxiv.org/html/2401.05856v1" in claims[0].text


def test_urls_with_query_strings_and_underscores_survive_extraction():
    report = (
        "## 官方公告\n\n"
        "新华社报道确认了该奖项的归属，详见[新华社](https://app.xinhuanet.com/news/article.html?articleId=abc123&x=1)"
        "以及[搜狐](https://www.sohu.com/a/1096647151_120678874)。\n"
    )
    claims = extract_claims(report)
    urls = [url for claim in claims for url in claim.cited_urls]
    assert "https://app.xinhuanet.com/news/article.html?articleId=abc123&x=1" in urls
    assert "https://www.sohu.com/a/1096647151_120678874" in urls
