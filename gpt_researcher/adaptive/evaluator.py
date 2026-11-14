"""Research quality evaluation (light by default, full on request).

``light`` mode runs zero LLM calls: claims are extracted with rules, and the
metrics are pure functions plus one sampled HTTP check. ``full`` mode adds two
LLM-judged metrics (evidence support, contradictions) behind an injectable
``judge`` callable so tests stay offline and the cost stays bounded.

See docs/改造方案/08_质量评估指标设计.md and 09_动态决策与终止机制.md.
"""

from __future__ import annotations

import inspect
import json
import logging
import math
from typing import Any, Awaitable, Callable, Sequence

import json_repair

from ..config import Config
from ..prompts import PromptFamily, get_prompt_family
from ..utils.llm import create_chat_completion
from .claims import extract_claims, extract_urls
from .metrics import (
    check_urls,
    compute_citation_match,
    compute_completeness,
    compute_coverage,
    compute_freshness,
    normalize_url,
)
from .state import (
    Claim,
    Deficit,
    EvaluationResult,
    Evidence,
    MetricResult,
    ResearchState,
    SupportLevel,
    TaskStatus,
)
from .storage import utc_now

logger = logging.getLogger(__name__)

REQUIRED_LLM_CALLS_MAX = 8
SUPPORT_BATCH_SIZE = 10
CONFLICT_SAMPLE_SIZE = 6
CONFLICT_PAIR_CAP = 10
MAX_EVIDENCE_PER_CLAIM = 3
SNIPPET_CHARS = 400

JudgeCall = Callable[[str], Awaitable[str]]


class QualityGate:
    """Decides whether an evaluation result is good enough to stop."""

    def __init__(self, cfg: Config | None = None) -> None:
        cfg = cfg or Config()
        self.min_coverage = float(getattr(cfg, "adaptive_min_coverage", 0.80))
        self.min_citation_match = float(getattr(cfg, "adaptive_min_citation_match", 0.80))
        self.min_completeness = float(getattr(cfg, "adaptive_min_completeness", 0.90))
        self.min_support_rate = float(getattr(cfg, "adaptive_min_support_rate", 0.85))
        self.max_conflict = float(getattr(cfg, "adaptive_max_conflict", 0.10))

    def check(self, evaluation: EvaluationResult, mode: str = "light") -> bool:
        required = {
            "coverage": self.min_coverage,
            "citation_match_url": self.min_citation_match,
            "completeness": self.min_completeness,
        }
        for name, floor in required.items():
            value = evaluation.value(name)
            if value is None or value < floor:
                return False
        if mode == "full":
            support = evaluation.value("support_rate")
            if support is None or support < self.min_support_rate:
                return False
            conflict = evaluation.value("conflict_rate")
            if conflict is not None and conflict > self.max_conflict:
                return False
        return True


class ResearchEvaluator:
    """Computes the metric set for one report draft."""

    def __init__(
        self,
        cfg: Config | None = None,
        prompt_family: PromptFamily | None = None,
        judge: JudgeCall | None = None,
        sampler=None,
        now=None,
    ) -> None:
        self.cfg = cfg if cfg is not None else Config()
        self.prompt_family = prompt_family or get_prompt_family(
            getattr(self.cfg, "prompt_family", "default"), self.cfg
        )
        self.judge = judge
        self.sampler = sampler
        self.now = now
        self.gate = QualityGate(self.cfg)
        self.validity_sample = int(getattr(self.cfg, "adaptive_validity_sample", 20))
        self.freshness_months = int(getattr(self.cfg, "adaptive_freshness_months", 24))

    # ------------------------------------------------------------------ public
    async def evaluate(
        self,
        state: ResearchState,
        report: str,
        mode: str | None = None,
        round_no: int | None = None,
    ) -> EvaluationResult:
        mode = mode or str(getattr(self.cfg, "adaptive_eval_mode", "light") or "light")
        if mode == "full" and self.judge is None:
            logger.info("no judge configured; full evaluation degrades to light")
            mode = "light"

        claims = extract_claims(report, state.plan)
        metrics: dict[str, MetricResult] = {
            "coverage": compute_coverage(state, claims),
            "citation_match_url": compute_citation_match(claims, state.evidence),
            "completeness": compute_completeness(report),
            "source_freshness": compute_freshness(
                state.evidence, months=self.freshness_months, now=self.now
            ),
            "source_validity": await self._validity(claims, state.evidence),
        }

        llm_calls = 0
        if mode == "full" and claims:
            llm_calls += await self._full_metrics(claims, state.evidence, metrics)

        result = EvaluationResult(
            round=round_no or (len(state.rounds) + 1),
            mode=mode,
            metrics=metrics,
            claims=claims,
            deficits=[],
            passed=False,
            evaluated_at=utc_now(),
            llm_calls=llm_calls,
        )
        result.deficits = self._deficits(state, result)
        result.passed = self.gate.check(result, mode)
        return result

    # ------------------------------------------------------------- light parts
    async def _validity(self, claims: Sequence[Claim], evidence: Sequence[Evidence]) -> MetricResult:
        urls = [url for claim in claims for url in claim.cited_urls] or [
            e.url for e in evidence if e.url
        ]
        return await check_urls(
            urls, sampler=self.sampler, sample=self.validity_sample
        )

    # -------------------------------------------------------------- full parts
    async def _full_metrics(
        self,
        claims: list[Claim],
        evidence: Sequence[Evidence],
        metrics: dict[str, MetricResult],
    ) -> int:
        calls = 0
        metrics["support_rate"], used = await self._support_rate(claims, evidence)
        calls += used
        if calls >= REQUIRED_LLM_CALLS_MAX:
            return calls
        metrics["citation_match_semantic"] = self._semantic_citation_match(claims)
        remaining = max(0, REQUIRED_LLM_CALLS_MAX - calls)
        if remaining:
            metrics["conflict_rate"], used = await self._conflict_rate(claims, remaining)
            calls += used
        return calls

    async def _support_rate(
        self, claims: list[Claim], evidence: Sequence[Evidence]
    ) -> tuple[MetricResult, int]:
        judged = supported = weak = unsupported = 0
        calls = 0
        for start in range(0, len(claims), SUPPORT_BATCH_SIZE):
            if calls >= math.ceil(REQUIRED_LLM_CALLS_MAX / 2):
                break
            batch = claims[start : start + SUPPORT_BATCH_SIZE]
            lines = self._render_claims_with_evidence(batch, evidence)
            prompt = self.prompt_family.generate_support_judge_prompt(lines)
            response = await self._ask(prompt)
            calls += 1
            entries = self._parse_json(response, expect=list)
            by_id = {c.id: c for c in batch}
            allowed = {
                claim.id: {self._evidence_id_for(e) for e in self._candidates(claim, evidence)}
                for claim in batch
            }
            for entry in entries or []:
                if not isinstance(entry, dict):
                    continue
                claim = by_id.get(str(entry.get("claim_id", "")))
                if claim is None:
                    continue
                verdict = str(entry.get("verdict", "")).lower()
                evidence_id = str(entry.get("evidence_id", "") or "")
                if verdict not in ("supported", "weak", "unsupported"):
                    verdict = "unchecked"
                if verdict != "unsupported" and evidence_id not in allowed.get(claim.id, set()):
                    verdict = "unchecked"
                claim.support = SupportLevel(verdict)
                claim.judge_reason = str(entry.get("reason", ""))[:300]
                if evidence_id:
                    claim.evidence_ids = [evidence_id]
                if verdict == "supported":
                    supported += 1
                elif verdict == "weak":
                    weak += 1
                elif verdict == "unsupported":
                    unsupported += 1
                judged += 1
        denominator = judged
        # A weak verdict counts half, so the score is not all-or-nothing.
        numerator = supported + weak * 0.5
        return (
            MetricResult(
                name="support_rate",
                value=(numerator / denominator) if denominator else None,
                numerator=numerator,
                denominator=denominator,
                method="llm",
                sampled=denominator < len(claims),
                notes=f"supported={supported} weak={weak} unsupported={unsupported}",
            ),
            calls,
        )

    def _semantic_citation_match(self, claims: Sequence[Claim]) -> MetricResult:
        """Citations that resolve to evidence AND whose claim is supported."""
        with_citation = [c for c in claims if c.cited_urls]
        if not with_citation:
            return MetricResult(name="citation_match_semantic", value=None, notes="no_citations")
        supported = [
            c
            for c in with_citation
            if c.support in (SupportLevel.SUPPORTED, SupportLevel.WEAK)
        ]
        return MetricResult(
            name="citation_match_semantic",
            value=len(supported) / len(with_citation),
            numerator=len(supported),
            denominator=len(with_citation),
            method="llm",
        )

    async def _conflict_rate(
        self, claims: list[Claim], max_calls: int
    ) -> tuple[MetricResult, int]:
        sample = self._even_sample(claims, CONFLICT_SAMPLE_SIZE)
        if len(sample) < 2:
            return MetricResult(name="conflict_rate", value=0.0, denominator=0), 0
        pairs = min(CONFLICT_PAIR_CAP, len(sample) * (len(sample) - 1) // 2)
        lines = "\n".join(f"- {c.id}: {c.text[:200]}" for c in sample)
        prompt = self.prompt_family.generate_conflict_judge_prompt(lines)
        response = await self._ask(prompt)
        payload = self._parse_json(response, expect=dict) or {}
        found = payload.get("conflicts") if isinstance(payload, dict) else None
        valid_ids = {c.id for c in sample}
        conflicts = [
            item
            for item in (found or [])
            if isinstance(item, dict)
            and str(item.get("a")) in valid_ids
            and str(item.get("b")) in valid_ids
        ]
        return (
            MetricResult(
                name="conflict_rate",
                value=len(conflicts) / pairs if pairs else None,
                numerator=len(conflicts),
                denominator=pairs,
                method="llm",
                sampled=len(sample) < len(claims),
                notes="conflicting_pairs="
                + ",".join(f"{c.get('a')}-{c.get('b')}" for c in conflicts[:5]),
            ),
            1 if max_calls else 0,
        )

    # ------------------------------------------------------------------ helpers
    def _render_claims_with_evidence(
        self, claims: Sequence[Claim], evidence: Sequence[Evidence]
    ) -> str:
        blocks = []
        for claim in claims:
            candidates = self._candidates(claim, evidence)
            evidence_lines = "\n".join(
                f"  [{self._evidence_id_for(e)}] {e.title[:80]}: {e.snippet[:SNIPPET_CHARS]}"
                for e in candidates
            )
            blocks.append(f"- {claim.id}: {claim.text}\n  evidence:\n{evidence_lines}")
        return "\n\n".join(blocks)

    def _candidates(self, claim: Claim, evidence: Sequence[Evidence]) -> list[Evidence]:
        cited = {normalize_url(u) for u in claim.cited_urls}
        same_source = [e for e in evidence if normalize_url(e.url) in cited]
        pool = same_source or list(evidence)
        return pool[:MAX_EVIDENCE_PER_CLAIM]

    @staticmethod
    def _evidence_id_for(evidence: Evidence) -> str:
        return evidence.id

    @staticmethod
    def _even_sample(items: Sequence[Claim], size: int) -> list[Claim]:
        if len(items) <= size:
            return list(items)
        step = len(items) / size
        return [items[int(i * step)] for i in range(size)]

    @staticmethod
    def _parse_json(response: str, expect: type):
        text = (response or "").strip()
        if not text:
            return None
        candidates = [text]
        if "```" in text:
            for part in text.split("```"):
                part = part.strip().removeprefix("json").strip()
                if part:
                    candidates.append(part)
        for candidate in candidates:
            try:
                value = json_repair.loads(candidate)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(value, expect):
                return value
        return None

    async def _ask(self, prompt: str) -> str:
        if self.judge is None:
            return ""
        try:
            result = self.judge(prompt)
            if inspect.isawaitable(result):
                return await result
            return str(result)
        except Exception as exc:  # noqa: BLE001 - a failed judge degrades, never crashes
            logger.warning("adaptive judge failed: %s", exc)
            return ""

    # ----------------------------------------------------------------- deficits
    def _deficits(self, state: ResearchState, evaluation: EvaluationResult) -> list[Deficit]:
        deficits: list[Deficit] = []

        coverage = evaluation.metric("coverage")
        if coverage and coverage.value is not None and coverage.value < self.gate.min_coverage:
            covered = {tid for c in evaluation.claims for tid in c.task_ids}
            for task in state.plan:
                if task.status is TaskStatus.SKIPPED:
                    continue
                if task.id not in covered or task.status is not TaskStatus.DONE:
                    deficits.append(
                        Deficit(
                            kind="missing_coverage",
                            description=f"任务 {task.id} 未被报告覆盖：{task.goal[:80]}",
                            task_id=task.id,
                            suggested_query=task.query,
                        )
                    )

        citation = evaluation.metric("citation_match_url")
        if citation and citation.value is not None and citation.value < self.gate.min_citation_match:
            evidence_urls = {normalize_url(e.url) for e in state.evidence if e.url}
            missing = {
                url
                for claim in evaluation.claims
                for url in claim.cited_urls
                if normalize_url(url) not in evidence_urls
            }
            for url in list(missing)[:3]:
                deficits.append(
                    Deficit(
                        kind="broken_citation",
                        description=f"引用无法回溯到已收集证据：{url}",
                        suggested_query=None,
                    )
                )

        completeness = evaluation.metric("completeness")
        if (
            completeness
            and completeness.value is not None
            and completeness.value < self.gate.min_completeness
        ):
            deficits.append(
                Deficit(
                    kind="incomplete_section",
                    description=f"报告结构不完整（{completeness.notes or 'missing parts'}）",
                )
            )

        for claim in evaluation.claims:
            if claim.support is SupportLevel.UNSUPPORTED:
                deficits.append(
                    Deficit(
                        kind="weak_evidence",
                        description=f"结论缺乏证据支持：{claim.text[:80]}",
                        claim_id=claim.id,
                        suggested_query=claim.text[:120],
                    )
                )

        conflict = evaluation.metric("conflict_rate")
        if conflict and conflict.value is not None and conflict.value > self.gate.max_conflict:
            deficits.append(
                Deficit(
                    kind="conflict",
                    description=f"存在 {int(conflict.numerator or 0)} 处结论冲突",
                )
            )
        return deficits


def default_judge(cfg: Config | None = None) -> JudgeCall:
    """Judge backed by the project's smart LLM (temperature 0)."""
    cfg = cfg or Config()

    async def _judge(prompt: str) -> str:
        return await create_chat_completion(
            model=cfg.smart_llm_model,
            messages=[{"role": "user", "content": prompt}],
            llm_provider=cfg.smart_llm_provider,
            temperature=0.0,
            max_tokens=min(int(getattr(cfg, "smart_token_limit", 4000)), 4000),
            llm_kwargs=cfg.llm_kwargs,
        )

    return _judge
