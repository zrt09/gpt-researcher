"""Outline-first report writer.

The legacy writer asked the model for a whole report in one shot, which produced
unstable structures (duplicate headings, opinion sections, missing summaries).
This writer splits the job:

    outline (4-6 sections) -> one call per section -> summary + conclusion -> assemble

Every part is bounded, section bodies are written in parallel, and the final
markdown is normalised (single H1, numbered sections, deterministic References).

On any failure the orchestrator falls back to the one-shot writer and finally to
a deterministic evidence digest, so a report always exists.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Sequence

import json_repair

from ..config import Config
from ..prompts import PromptFamily, get_prompt_family
from ..utils.llm import create_chat_completion
from .state import Evidence, ResearchState
from .textutil import (
    detect_language,
    normalize_markdown,
    number_sections,
    sanitize_citations,
    truncate_words,
)

logger = logging.getLogger(__name__)

MIN_SECTIONS = 4
MAX_SECTIONS = 7
SECTION_CONCURRENCY = 3
EVIDENCE_PER_SECTION = 6
SNIPPET_CHARS = 700
MAX_REFERENCES = 40

_LABELS = {
    "en": {
        "summary": "Executive Summary",
        "conclusion": "Conclusion",
        "references": "References",
        "section_words": "150-250 words",
        "summary_words": "at most 160 words",
        "conclusion_words": "at most 140 words",
        "language": "English",
    },
    "zh": {
        "summary": "摘要",
        "conclusion": "结论",
        "references": "参考文献",
        "section_words": "300-500 字",
        "summary_words": "不超过 250 字",
        "conclusion_words": "不超过 220 字",
        "language": "简体中文",
    },
}

_FALLBACK_OUTLINE = {
    "en": [
        ("Background", "What the reader needs to know to follow the findings"),
        ("Key Findings", "The concrete documented findings that answer the question"),
        ("Mechanism and Evidence", "How the findings were established and what supports them"),
        ("Significance", "Why the findings matter and what they change"),
        ("Open Questions", "What remains unresolved or contested"),
    ],
    "zh": [
        ("背景", "理解后续发现所需的最小背景"),
        ("核心发现", "直接回答问题的事实性发现"),
        ("机制与证据", "这些发现是如何得出的、有哪些证据"),
        ("影响与意义", "这些发现为什么重要、改变了什么"),
        ("未解决的问题", "仍然存在争议或未解决的部分"),
    ],
}


@dataclass
class SectionSpec:
    title: str
    purpose: str = ""


class StructuredWriter:
    """Writes a report as outline + sections instead of one giant prompt."""

    def __init__(
        self,
        cfg: Config | None = None,
        prompt_family: PromptFamily | None = None,
        llm_call=None,
        max_sections: int = MAX_SECTIONS,
        concurrency: int = SECTION_CONCURRENCY,
    ) -> None:
        self.cfg = cfg if cfg is not None else Config()
        self.prompt_family = prompt_family or get_prompt_family(
            getattr(self.cfg, "prompt_family", "default"), self.cfg
        )
        self._llm = llm_call
        self.max_sections = max_sections
        self.concurrency = max(1, concurrency)

    # ------------------------------------------------------------------ public
    async def __call__(self, state: ResearchState, notes: Sequence[str] = ()) -> str:
        lang = detect_language(state.query)
        if not state.evidence:
            return ""

        title, specs = await self._outline(state, lang)
        bodies = await self._write_sections(state, specs, lang, notes)
        if not any(body.strip() for body in bodies.values()):
            return ""

        summary = await self._summary(state, title, specs, bodies, lang)
        conclusion = await self._conclusion(state, bodies, lang)
        report = self._assemble(state, title, specs, bodies, summary, conclusion, lang)

        url_titles: dict[str, str] = {}
        for item in state.evidence:
            if item.url and item.url not in url_titles:
                url_titles[item.url] = item.title or item.url
        report, changed = sanitize_citations(report, url_titles)
        if changed:
            logger.info("sanitized %d uncited link(s) in the report", changed)

        return number_sections(normalize_markdown(report, title=title))

    # -------------------------------------------------------------------- llm
    async def _complete(self, prompt: str) -> str:
        if self._llm is not None:
            return await self._llm(prompt)
        return await create_chat_completion(
            model=self.cfg.smart_llm_model,
            messages=[{"role": "user", "content": prompt}],
            llm_provider=self.cfg.smart_llm_provider,
            temperature=0.3,
            max_tokens=min(int(getattr(self.cfg, "smart_token_limit", 4000)), 4000),
            llm_kwargs=self.cfg.llm_kwargs,
        )

    # ----------------------------------------------------------------- outline
    async def _outline(self, state: ResearchState, lang: str) -> tuple[str, list[SectionSpec]]:
        labels = _LABELS[lang]
        sources = "\n".join(
            f"- {item.title or item.url}" for item in state.evidence[:24]
        )
        prompt = f"""You are designing the structure of a research report.

Research question: {state.query}

Available evidence titles:
{sources}

Design {MIN_SECTIONS} to {self.max_sections} sections that together answer the question.
Rules:
- Sections must not overlap and must be ordered logically.
- Do NOT include an introduction/summary, conclusion, references or "opinion" section: they are added automatically.
- Titles are short noun phrases without numbering.
- Write titles and purposes in {labels['language']}.

Return ONLY JSON:
{{"title": "<report title>", "sections": [{{"title": "<section title>", "purpose": "<one sentence: what this section must establish>"}}]}}
"""
        try:
            payload = self._parse_json(await self._complete(prompt), dict)
            title = str(payload.get("title") or state.query).strip()
            raw_sections = payload.get("sections") or []
            specs = [
                SectionSpec(str(s.get("title", "")).strip(), str(s.get("purpose", "")).strip())
                for s in raw_sections
                if isinstance(s, dict) and str(s.get("title", "")).strip()
            ][: self.max_sections]
            if len(specs) >= MIN_SECTIONS - 1:
                return title or state.query, specs
            logger.warning("outline returned %d sections; using fallback", len(specs))
        except Exception as exc:  # noqa: BLE001 - a bad outline is not fatal
            logger.warning("outline generation failed: %s", exc)

        return state.query, [SectionSpec(t, p) for t, p in _FALLBACK_OUTLINE[lang]]

    # ----------------------------------------------------------------- sections
    async def _write_sections(
        self,
        state: ResearchState,
        specs: list[SectionSpec],
        lang: str,
        notes: Sequence[str],
    ) -> dict[str, str]:
        labels = _LABELS[lang]
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(spec: SectionSpec) -> tuple[str, str]:
            async with semaphore:
                prompt = self._section_prompt(state, spec, specs, lang, notes, labels)
                try:
                    body = await self._complete(prompt)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("section %r failed: %s", spec.title, exc)
                    body = ""
                return spec.title, self._strip_section_heading(body, spec.title)

        results = await asyncio.gather(*[one(spec) for spec in specs])
        return dict(results)

    def _section_prompt(
        self,
        state: ResearchState,
        spec: SectionSpec,
        specs: list[SectionSpec],
        lang: str,
        notes: Sequence[str],
        labels: dict[str, str],
    ) -> str:
        evidence = self._select_evidence(state, spec)
        evidence_block = "\n".join(
            f"- [{item.title or item.url}]({item.url}) {truncate_words(item.snippet, 120)}"
            for item in evidence
        )
        sibling_titles = " / ".join(s.title for s in specs if s.title != spec.title)
        notes_block = ""
        if notes:
            notes_block = "\nFix these issues reported by the previous evaluation:\n" + "\n".join(
                f"- {note}" for note in notes
            )
        return f"""Write one section of a research report.

Report question: {state.query}
Section title: {spec.title}
Section purpose: {spec.purpose or spec.title}
Other sections (do not repeat their content): {sibling_titles}

Rules:
- Write in {labels['language']}.
- Length: {labels['section_words']}.
- Use ONLY the evidence below; cite it inline as [short name](url) right after the claim it supports.
- **Copy the URL exactly as given in the evidence list. Never invent, shorten or modify a URL.**
- Do NOT add a top-level title or repeat the section title as a heading.
- You may use at most 2 "###" subheadings.
- No summary of the whole report, no conclusion, no "as an AI" phrasing.

Evidence:
{evidence_block}
{notes_block}
"""

    # -------------------------------------------------------------- summary etc.
    async def _summary(
        self,
        state: ResearchState,
        title: str,
        specs: list[SectionSpec],
        bodies: dict[str, str],
        lang: str,
    ) -> str:
        labels = _LABELS[lang]
        digests = "\n".join(
            f"- {spec.title}: {truncate_words(bodies.get(spec.title, ''), 45)}"
            for spec in specs
            if bodies.get(spec.title)
        )
        prompt = f"""Write the executive summary of a research report.

Report title: {title}
Question: {state.query}
Sections:
{digests}

Rules:
- Write in {labels['language']}.
- Length: {labels['summary_words']}.
- State the answer to the question first, then the 3-4 key findings.
- Cite at most two sources with markdown links taken from the section texts.
- No heading, no bullet lists, no "this report" meta-commentary.
"""
        try:
            return (await self._complete(prompt)).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("summary generation failed: %s", exc)
            return ""

    async def _conclusion(
        self, state: ResearchState, bodies: dict[str, str], lang: str
    ) -> str:
        labels = _LABELS[lang]
        digest = "\n".join(truncate_words(body, 35) for body in bodies.values() if body)
        prompt = f"""Write the conclusion of a research report.

Question: {state.query}
Section texts (abridged):
{digest}

Rules:
- Write in {labels['language']}.
- Length: {labels['conclusion_words']}.
- Answer the question directly and state what remains uncertain.
- No heading, no new citations, no repetition of the summary wording.
"""
        try:
            return (await self._complete(prompt)).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("conclusion generation failed: %s", exc)
            return ""

    # ------------------------------------------------------------------ assembles
    def _assemble(
        self,
        state: ResearchState,
        title: str,
        specs: list[SectionSpec],
        bodies: dict[str, str],
        summary: str,
        conclusion: str,
        lang: str,
    ) -> str:
        labels = _LABELS[lang]
        parts: list[str] = [f"# {title}", ""]
        if summary:
            parts += [f"## {labels['summary']}", "", summary, ""]
        for spec in specs:
            body = bodies.get(spec.title, "").strip()
            if not body:
                continue
            parts += [f"## {spec.title}", "", body, ""]
        if conclusion:
            parts += [f"## {labels['conclusion']}", "", conclusion, ""]
        parts += [f"## {labels['references']}", ""]
        parts += self._reference_lines(state)
        return "\n".join(parts).strip() + "\n"

    @staticmethod
    def _reference_lines(state: ResearchState) -> list[str]:
        seen: set[str] = set()
        lines: list[str] = []
        for item in state.evidence:
            if not item.url or item.url in seen:
                continue
            seen.add(item.url)
            name = (item.title or item.url).replace("[", "(").replace("]", ")")
            lines.append(f"- [{name}]({item.url})")
            if len(lines) >= MAX_REFERENCES:
                break
        return lines or ["- （暂无来源）"]

    # ------------------------------------------------------------------ helpers
    def _select_evidence(self, state: ResearchState, spec: SectionSpec) -> list[Evidence]:
        keywords = self._keywords(f"{spec.title} {spec.purpose}")
        scored: list[tuple[int, Evidence]] = []
        for item in state.evidence:
            text = f"{item.title} {item.snippet}".lower()
            score = sum(1 for word in keywords if word and word in text)
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        chosen = [item for score, item in scored if score > 0][:EVIDENCE_PER_SECTION]
        if len(chosen) < 2:
            chosen = [item for _, item in scored[:EVIDENCE_PER_SECTION]]
        return chosen

    @staticmethod
    def _keywords(text: str) -> set[str]:
        words = re.findall(r"[\w\u4e00-\u9fff]{2,}", (text or "").lower())
        return {word for word in words if len(word) >= 3 or re.search(r"[\u4e00-\u9fff]", word)}

    @staticmethod
    def _strip_section_heading(body: str, title: str) -> str:
        """Drop a leading H1/H2 from a section body.

        The assembler adds the section heading, so any top-level heading the
        model wrote itself would duplicate it (even if the wording differs).
        ``###`` subheadings are kept.
        """
        lines = (body or "").strip().split("\n")
        if lines:
            first = lines[0].strip()
            if (first.startswith("## ") or first.startswith("# ")) and not first.startswith("###"):
                lines = lines[1:]
        cleaned = "\n".join(lines).strip()
        cleaned = re.sub(r"^\s*(?:---|\*\*\*)\s*$", "", cleaned, flags=re.MULTILINE)
        return cleaned.strip()

    @staticmethod
    def _parse_json(text: str, expect: type) -> Any:
        raw = (text or "").strip()
        candidates = [raw]
        for block in re.findall(r"```(?:json)?\s*([\s\S]*?)```", raw):
            candidates.append(block.strip())
        match = re.search(r"\{[\s\S]*\}", raw)
        if match:
            candidates.append(match.group(0))
        for candidate in candidates:
            if not candidate:
                continue
            try:
                value = json_repair.loads(candidate)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(value, expect):
                return value
        raise ValueError("no valid JSON object in outline response")

