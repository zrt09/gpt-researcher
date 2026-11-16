"""Small text helpers shared by the adaptive writer and report assembly."""

from __future__ import annotations

import re

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_NUMBERED_H2_RE = re.compile(r"^##\s+(\d+)[.)]?\s+")
_H2_RE = re.compile(r"^##\s+(?!#)(.+?)\s*$")
_H1_RE = re.compile(r"^#\s+(?!#)(.+?)\s*$")
_RESERVED_H2 = (
    "executive summary",
    "summary",
    "abstract",
    "conclusion",
    "conclusions",
    "references",
    "appendix",
    "摘要",
    "概述",
    "结论",
    "总结",
    "参考文献",
    "附录",
)


def detect_language(text: str, default: str = "en") -> str:
    """Return ``zh`` when the text contains a meaningful share of CJK characters."""
    sample = (text or "")[:2000]
    if not sample:
        return default
    cjk = len(_CJK_RE.findall(sample))
    return "zh" if cjk / max(1, len(sample)) >= 0.15 else "en"


def heading_text(markdown: str) -> list[str]:
    return [line.strip() for line in (markdown or "").splitlines() if line.startswith("#")]


def normalize_markdown(markdown: str, *, title: str | None = None) -> str:
    """Make the heading tree sane: one H1, no empty headings, no stray blank runs."""
    text = (markdown or "").replace("\r\n", "\n").lstrip("\ufeff").strip()
    if not text:
        return ""

    lines: list[str] = []
    seen_h1 = False
    for raw in text.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            body = stripped[hashes:].strip()
            if not body:
                continue
            if hashes == 1:
                if seen_h1:
                    line = f"## {body}"
                else:
                    seen_h1 = True
                    line = f"# {body}"
            else:
                line = f"{'#' * hashes} {body}"
        lines.append(line)

    if not seen_h1:
        lines.insert(0, f"# {title}" if title else "# Research Report")
        lines.insert(1, "")

    collapsed: list[str] = []
    blank_run = 0
    for line in lines:
        if not line.strip():
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        collapsed.append(line)
    return "\n".join(collapsed).strip() + "\n"


def number_sections(markdown: str) -> str:
    """Prefix unnumbered top-level sections with ``1.``, ``2.``... in order."""
    counter = 0
    out: list[str] = []
    for line in (markdown or "").split("\n"):
        match = _H2_RE.match(line.strip())
        if match:
            title = match.group(1).strip()
            lowered = title.lower()
            if any(lowered.startswith(word) for word in _RESERVED_H2):
                out.append(line)
                continue
            if _NUMBERED_H2_RE.match(line.strip()):
                counter = max(counter, int(_NUMBERED_H2_RE.match(line.strip()).group(1)))
                out.append(line)
                continue
            counter += 1
            out.append(f"## {counter}. {title}")
            continue
        out.append(line)
    return "\n".join(out)


def strip_reference_section(markdown: str) -> str:
    """Remove everything from a References heading onwards (kept for assembly)."""
    lines = (markdown or "").split("\n")
    for index, line in enumerate(lines):
        match = _H2_RE.match(line.strip())
        if not match:
            continue
        title = match.group(1).strip().lower()
        if title in ("references", "参考文献", "reference"):
            return "\n".join(lines[:index]).rstrip()
    return (markdown or "").rstrip()


def truncate_words(text: str, max_words: int) -> str:
    words = (text or "").split()
    return " ".join(words[:max_words]) if len(words) > max_words else (text or "")


_LINK_RE = re.compile(r"\[(?P<label>[^\]]+)\]\((?P<url>https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(
    r"(?<![\(\[])(?<!\]\()https?://[^\s<>\)\]\"'，。；：！？、\u4e00-\u9fff]+"
)


def sanitize_citations(
    markdown: str,
    url_titles: dict[str, str],
    min_label_similarity: float = 55.0,
) -> tuple[str, int]:
    """Drop links to URLs that were never collected.

    A language model can invent, truncate or slightly rewrite a URL; keeping it
    makes the report cite a page that was never read. Two passes:

    1. markdown links: unknown targets are re-pointed at the evidence whose title
       matches the link label, and stripped to plain text when nothing matches;
    2. bare URLs (models sometimes drop the link syntax): unknown ones are
       removed, because there is no label to re-point with.

    ``url_titles`` maps original evidence URL -> its title.
    Returns the cleaned markdown and the number of changed links.
    """
    from rapidfuzz import fuzz

    from .metrics import normalize_url

    if not markdown:
        return markdown, 0

    known = {normalize_url(url) for url in url_titles}
    changed = 0

    def replace(match: re.Match) -> str:
        nonlocal changed
        label = match.group("label")
        url = match.group("url")
        if normalize_url(url) in known:
            return match.group(0)
        changed += 1
        best_url, best_score = None, min_label_similarity
        for candidate, title in url_titles.items():
            score = fuzz.partial_ratio(label.lower(), (title or "").lower())
            if score >= best_score:
                best_url, best_score = candidate, score
        if best_url is not None:
            return f"[{label}]({best_url})"
        return label

    cleaned = _LINK_RE.sub(replace, markdown)

    def replace_bare(match: re.Match) -> str:
        nonlocal changed
        url = match.group(0)
        if normalize_url(url) in known:
            return url
        changed += 1
        return ""

    cleaned = _BARE_URL_RE.sub(replace_bare, cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    return cleaned, changed
