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
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def text_similarity(left: str, right: str) -> float:
    """0..100 lexical similarity that also works for Chinese.

    ``token_set_ratio`` is meaningless for Chinese (no spaces), so character
    bigrams are used as an additional signal.
    """
    a = (left or "").strip().lower()
    b = (right or "").strip().lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    score = float(fuzz.token_set_ratio(a, b))
    if _CJK_RE.search(a) or _CJK_RE.search(b):
        grams_a, grams_b = _bigrams(a), _bigrams(b)
        if grams_a and grams_b:
            # Dice coefficient copes with Chinese text where one string is a
            # shorter paraphrase of the other.
            dice = 200.0 * len(grams_a & grams_b) / (len(grams_a) + len(grams_b))
            score = max(score, dice)
    return score

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s*(?P<title>.+?)\s*$")
_URL_RE = re.compile(r"https?://[^\s<>\)\]\"'，。；：！？、）】》＞\u4e00-\u9fff]+")
_SENTENCE_END = re.compile(
    r"(?<=[。！？；])\s*|(?<=[.!?;])\s+(?=[A-Z0-9(\"'\u4e00-\u9fff])"
)
_REFERENCE_TITLES = ("reference", "references", "参考文献", "参考来源", "引用来源", "sources")
_INTRO_TITLES = (
    "introduction",
    "executive summary",
    "summary",
    "overview",
    "abstract",
    "背景",
    "引言",
    "简介",
    "前言",
    "摘要",
    "概述",
    "导言",
)
_CONCLUSION_TITLES = ("conclusion", "conclusions", "结论", "总结", "结语", "展望")
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
        url = raw.rstrip(".,;:!?，。；：！？、）】》＞\"'")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _sentences(body: str) -> list[str]:
    """Split a section body into raw sentence pieces (no cleaning yet).

    Cleaning happens per piece, but URLs are extracted from the raw piece so
    markdown emphasis stripping can never damage a URL.
    """
    parts: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for piece in _SENTENCE_END.split(stripped):
            if piece.strip():
                parts.append(piece)
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
        goal = task.goal or ""
        query = task.query or ""
        score = max(
            text_similarity(section_title, goal),
            text_similarity(section_title, query),
            text_similarity(sentence, goal),
            text_similarity(sentence, query),
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
        for raw in _sentences(section.body):
            sentence = _clean(raw)
            if not _is_candidate(sentence):
                continue
            claims.append(
                Claim(
                    id=f"C{len(claims) + 1}",
                    text=sentence,
                    section=section.title,
                    cited_urls=extract_urls(raw),
                    task_ids=_attribute(sentence, section.title, task_list),
                )
            )
            kept_in_section += 1
            if kept_in_section >= per_section or len(claims) >= max_claims:
                break
        if len(claims) >= max_claims:
            break
    return claims
