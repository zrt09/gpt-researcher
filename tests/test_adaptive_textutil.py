"""Tests for report structure helpers."""

from gpt_researcher.adaptive.textutil import (
    detect_language,
    normalize_markdown,
    number_sections,
    sanitize_citations,
    strip_reference_section,
    truncate_words,
)


def test_detect_language_chinese_and_english():
    assert detect_language("2026年诺贝尔物理学奖得主及其主要贡献") == "zh"
    assert detect_language("What are the limitations of RAG systems?") == "en"
    assert detect_language("") == "en"


def test_normalize_markdown_demotes_extra_h1():
    text = "# First\n\n# Second\n\n## Section\n"
    out = normalize_markdown(text)
    assert out.count("\n# ") + out.startswith("# ") == 1
    assert "## Second" in out


def test_normalize_markdown_creates_title_when_missing():
    out = normalize_markdown("## Section\n\nBody\n", title="My Title")
    assert out.startswith("# My Title")


def test_normalize_markdown_removes_empty_headings_and_blank_runs():
    out = normalize_markdown("# T\n\n##\n\n\n\n## Real\n\nBody\n")
    assert "## Real" in out
    assert "\n\n\n" not in out


def test_number_sections_numbers_only_body_sections():
    text = (
        "# T\n\n## Executive Summary\n\ns\n\n"
        "## Background\n\nb\n\n## Findings\n\nf\n\n"
        "## Conclusion\n\nc\n\n## References\n\n- x\n"
    )
    out = number_sections(text)
    assert "## 1. Background" in out
    assert "## 2. Findings" in out
    assert "## Executive Summary" in out
    assert "## Conclusion" in out
    assert "## References" in out


def test_number_sections_does_not_double_number():
    out = number_sections("## 1. Background\n\n## Findings\n")
    assert "## 1. Background" in out
    assert "## 2. Findings" in out
    assert "## 1. 1." not in out


def test_strip_reference_section_keeps_body():
    text = "# T\n\nBody\n\n## References\n\n- a\n"
    assert strip_reference_section(text) == "# T\n\nBody"


def test_truncate_words():
    assert truncate_words("a b c d", 2) == "a b"
    assert truncate_words("a b", 5) == "a b"


def test_sanitize_citations_keeps_known_links():
    text = "Claim ([Source 1](https://e.com/1))."
    cleaned, changed = sanitize_citations(text, {"https://e.com/1": "Source 1"})
    assert cleaned == text
    assert changed == 0


def test_sanitize_citations_repoints_unknown_url_by_label():
    text = "Claim ([Source 1](https://wrong.example/x))."
    cleaned, changed = sanitize_citations(text, {"https://e.com/1": "Source 1"})
    assert "https://e.com/1" in cleaned
    assert "wrong.example" not in cleaned
    assert changed == 1


def test_sanitize_citations_strips_unmatchable_links():
    text = "Claim ([Totally different](https://wrong.example/x))."
    cleaned, changed = sanitize_citations(text, {"https://e.com/1": "Source 1"})
    assert cleaned == "Claim (Totally different)."
    assert changed == 1


def test_sanitize_citations_ignores_bare_text():
    cleaned, changed = sanitize_citations("No links here.", {})
    assert cleaned == "No links here."
    assert changed == 0


def test_sanitize_citations_removes_unknown_bare_urls():
    text = "来源：https://wrong.example/x 显示该结果。"
    cleaned, changed = sanitize_citations(text, {"https://e.com/1": "Source 1"})
    assert "wrong.example" not in cleaned
    assert "来源：" in cleaned
    assert changed == 1


def test_sanitize_citations_keeps_known_bare_urls():
    text = "来源：https://e.com/1 显示该结果。"
    cleaned, changed = sanitize_citations(text, {"https://e.com/1": "Source 1"})
    assert "https://e.com/1" in cleaned
    assert changed == 0
