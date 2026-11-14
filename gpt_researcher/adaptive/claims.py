"""Structured claim extraction from a research report.

Light-mode evaluation must cost zero LLM calls, so claims are found with plain
markdown/text rules: split into sections, split into sentences, keep declarative
sentences, capture the URLs they cite, and attribute them to plan tasks by
lexical similarity.

This is intentionally conservative: a sentence that cannot be attributed to a
task simply has no ``task_ids``, which lowers measured coverage rather than
inventing a match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from rapidfuzz import fuzz

from .state import Claim, SubTask, TaskStatus

MAX_CLAIMS = 80
MAX_CLAIMS_PER_SECTION = 12
MIN_CLAIM_CHARS = 20
TASK_ATTRIBUTION_THRESHOLD = 65.0
MAX_TASKS_PER_CLAIM = 2

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s*(?P<title>.+?)\s*$")
_URL_RE = re.compile(r"https?://[^\s<>\)\]\"']+")
_SENTENCE_END = re.compile(r"(?<=[。！？；!?;])\s*|(?<=[.])\s+(?=[A-Z0-9(\"'])")
_REFERENCE_TITLES = ("reference", "references", "参考文献", "参考来源", "引用来源", "sources")
_INTRO_TITLES = ("introduction", "引言", "简介", "前言")
_CONCLUSION_TITLES = ("conclusion", "结论", "总结", "结语")
_SKIP_PREFIXES = ("|", ">", "![", "[![")


@dataclass
class Section:
    title: str
    body: str
    level: int


def split_sections(report: str) -> list[Section]:
    """Split markdown into sections, preserving raw text (URLs included)."""
    if not report or not isinstance(report, str):
        return []

    sections: list[Section] = []
    title, level, buffer = "", 0, []
    for line in report.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match:
            if buffer or title:
                sections.append(Section(title, "\n".join(buffer).strip(), level))
            title = match.group("title").strip()
            level = len(match.group("hashes"))
            buffer = []
        else:
            buffer.append(line)
    if buffer or title:
        sections.append(Section(title, "\n".join(buffer).strip(), level))
    return sections


def is_reference_section(title: str) -> bool:
    lowered = (title or "").lower()
    return any(marker in lowered for marker in _REFERENCE_TITLES)


def has_intro_section(sections: Sequence[Section]) -> bool:
    return any(any(m in s.title.lower() for m in _INTRO_TITLES) for s in sections)


def has_conclusion_section(sections: Sequence[Section]) -> bool:
    return any(any(m in s.title.lower() for m in _CONCLUSION_TITLES) for s in sections)


def has_reference_section(sections: Sequence[Section]) -> bool:
    return any(is_reference_section(s.title) for s in sections)


def extract_urls(text: str) -> list[str]:
    """All URLs in a text, de-duplicated, order preserved, punctuation trimmed."""
    seen: set[str] = set()
    urls: list[str] = []
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:!?，。；：！？")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _sentences(body: str) -> list[str]:
    parts: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for piece in _SENTENCE_END.split(stripped):
            cleaned = _clean(piece)
            if cleaned:
                parts.append(cleaned)
    return parts


def _clean(text: str) -> str:
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"^\s*(?:[-+*]|\d+[.)])\s+", "", text)
    # Keep the URL: markdown links are the only place some reports carry their
    # citations, and dropping them makes citation matching always fail.
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 \2", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_candidate(sentence: str) -> bool:
    if len(sentence) < MIN_CLAIM_CHARS:
        return False
    if sentence.startswith(_SKIP_PREFIXES):
        return False
    if re.fullmatch(r"[\W_]+", sentence):
        return False
    # A bare URL list is reference material, not a claim.
    without_urls = _URL_RE.sub("", sentence).strip()
    return len(without_urls) >= MIN_CLAIM_CHARS // 2


def _attribute(sentence: str, section_title: str, tasks: Sequence[SubTask]) -> list[str]:
    scored: list[tuple[float, str]] = []
    for task in tasks:
        if task.status is TaskStatus.SKIPPED:
            continue
        score = max(
            fuzz.token_set_ratio(section_title.lower(), (task.goal or "").lower()),
            fuzz.token_set_ratio(section_title.lower(), task.query.lower()),
            fuzz.token_set_ratio(sentence.lower(), (task.goal or "").lower()),
            fuzz.token_set_ratio(sentence.lower(), task.query.lower()),
        )
        if score >= TASK_ATTRIBUTION_THRESHOLD:
            scored.append((float(score), task.id))
    scored.sort(reverse=True)
    return [task_id for _, task_id in scored[:MAX_TASKS_PER_CLAIM]]


def extract_claims(
    report: str,
    tasks: Iterable[SubTask] = (),
    max_claims: int = MAX_CLAIMS,
    per_section: int = MAX_CLAIMS_PER_SECTION,
) -> list[Claim]:
    """Extract at most ``max_claims`` declarative sentences from ``report``."""
    task_list = list(tasks)
    claims: list[Claim] = []
    for section in split_sections(report):
        if is_reference_section(section.title) or section.level == 0:
            continue
        kept_in_section = 0
        for sentence in _sentences(section.body):
            if not _is_candidate(sentence):
                continue
            claims.append(
                Claim(
                    id=f"C{len(claims) + 1}",
                    text=sentence,
                    section=section.title,
                    cited_urls=extract_urls(sentence),
                    task_ids=_attribute(sentence, section.title, task_list),
                )
            )
            kept_in_section += 1
            if kept_in_section >= per_section or len(claims) >= max_claims:
                break
        if len(claims) >= max_claims:
            break
    return claims
