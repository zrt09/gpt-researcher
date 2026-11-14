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
