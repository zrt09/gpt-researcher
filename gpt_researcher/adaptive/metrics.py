"""Quality metrics for the adaptive loop.

Every metric returns a :class:`MetricResult` carrying numerator/denominator so a
value can be audited instead of trusted. A zero denominator means "cannot be
computed" and yields ``value=None`` - never a perfect score.

Four metrics are pure functions (no network, no LLM):

* coverage           - planned tasks that the report actually answers
* citation_match     - cited URLs that exist in the collected evidence
* completeness       - required report structure is present and supported
* source_freshness   - dated sources inside the recency window

``source_validity`` performs HTTP checks, but through an injectable sampler so
tests never touch the network. See docs/改造方案/08_质量评估指标设计.md.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Sequence
from urllib.parse import urlsplit, urlunsplit

from .claims import (
    extract_urls,
    has_conclusion_section,
    has_intro_section,
    has_reference_section,
    is_reference_section,
    split_sections,
)
from .state import Claim, Evidence, MetricResult, ResearchState, TaskStatus

VALID_STATUSES = ("ok", "blocked", "invalid", "error")
MIN_BODY_CHARS = 120
MIN_BODY_SECTIONS = 2
PAGE_SCHEMES = ("http", "https")


def normalize_url(url: str) -> str:
    """Normalise a URL so two spellings of the same page compare equal."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    if not host:
        return url.strip()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, host, path, parts.query, ""))


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = normalize_url(value)
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


# --------------------------------------------------------------------- coverage


def compute_coverage(state: ResearchState, claims: Sequence[Claim]) -> MetricResult:
    """Share of planned (non-skipped) tasks that are done AND answered.

    A task counts as covered when either its id is attributed to a claim, or the
    report cites a URL that this task actually collected. The URL path matters:
    lexical attribution is a conservative lower bound, and a report section can
    answer a task without reusing the task's wording.
    """
    tasks = [t for t in state.plan if t.status is not TaskStatus.SKIPPED]
    if not tasks:
        return MetricResult(name="coverage", value=None, notes="no_tasks")

    task_urls: dict[str, set[str]] = {}
    for evidence in state.evidence:
        if evidence.url:
            task_urls.setdefault(evidence.task_id, set()).add(normalize_url(evidence.url))

    covered_ids: set[str] = set()
    for claim in claims:
        covered_ids.update(claim.task_ids)
        cited = {normalize_url(url) for url in claim.cited_urls}
        if not cited:
            continue
        for task_id, urls in task_urls.items():
            if cited & urls:
                covered_ids.add(task_id)

    covered = [t for t in tasks if t.status is TaskStatus.DONE and t.id in covered_ids]
    uncovered = [t.id for t in tasks if t.id not in {c.id for c in covered}]
    return MetricResult(
        name="coverage",
        value=_ratio(len(covered), len(tasks)),
        numerator=len(covered),
        denominator=len(tasks),
        notes="uncovered=" + ",".join(uncovered[:8]),
    )


# ---------------------------------------------------------------- citation match


def compute_citation_match(claims: Sequence[Claim], evidence: Sequence[Evidence]) -> MetricResult:
    """Share of cited URLs that can be traced back to collected evidence."""
    cited = [url for claim in claims for url in claim.cited_urls]
    unique_cited = _dedupe(cited)
    if not unique_cited:
        return MetricResult(name="citation_match_url", value=None, notes="no_citations")

    evidence_urls = {normalize_url(e.url) for e in evidence if e.url}
    matched = [u for u in unique_cited if normalize_url(u) in evidence_urls]
    missing = [u for u in unique_cited if normalize_url(u) not in evidence_urls]
    return MetricResult(
        name="citation_match_url",
        value=_ratio(len(matched), len(unique_cited)),
        numerator=len(matched),
        denominator=len(unique_cited),
        notes="missing=" + ",".join(missing[:3]),
    )


# ------------------------------------------------------------------- completeness


def compute_completeness(report: str) -> MetricResult:
    """Four structural requirements: intro, >=2 supported body sections, conclusion, references."""
    sections = split_sections(report)
    supported_body = [
        s
        for s in sections
        if not is_reference_section(s.title)
        and len(s.body) >= MIN_BODY_CHARS
        and len(extract_urls(s.body)) > 0
    ]
    checks = {
        "intro": has_intro_section(sections),
        "body": len(supported_body) >= MIN_BODY_SECTIONS,
        "conclusion": has_conclusion_section(sections),
        "references": has_reference_section(sections),
    }
    satisfied = sum(1 for ok in checks.values() if ok)
    missing = [name for name, ok in checks.items() if not ok]
    return MetricResult(
        name="completeness",
        value=_ratio(satisfied, len(checks)),
        numerator=satisfied,
        denominator=len(checks),
        notes="missing=" + ",".join(missing) if missing else "",
    )


# ---------------------------------------------------------------------- freshness


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    match = re.match(r"^(19|20)\d{2}$", text)
    if match:
        return datetime(int(text), 1, 1, tzinfo=timezone.utc)
    return None


def compute_freshness(
    evidence: Sequence[Evidence],
    months: int = 24,
    now: datetime | None = None,
) -> MetricResult:
    """Share of *dated* evidence published inside the recency window."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=int(months * 30))
    dated = 0
    fresh = 0
    for item in evidence:
        published = _parse_date(item.published_at)
        if published is None:
            continue
        dated += 1
        if published >= cutoff:
            fresh += 1
    if dated == 0:
        return MetricResult(
            name="source_freshness",
            value=None,
            notes=f"no_dates (evidence={len(evidence)})",
        )
    return MetricResult(
        name="source_freshness",
        value=_ratio(fresh, dated),
        numerator=fresh,
        denominator=dated,
        notes=f"dated={dated}/{len(evidence)}",
    )


# ---------------------------------------------------------------------- validity


def _even_sample(items: Sequence[str], size: int) -> list[str]:
    if size <= 0 or not items:
        return []
    if len(items) <= size:
        return list(items)
    step = len(items) / size
    return [items[int(i * step)] for i in range(size)]


async def http_sampler(url: str, timeout: float = 5.0) -> str:
    """Classify one URL: ok / blocked / invalid / error."""
    try:
        import httpx
    except Exception:  # pragma: no cover - httpx is a hard dependency
        return "error"

    headers = {"User-Agent": "Mozilla/5.0 (compatible; gpt-researcher-eval/1.0)"}
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=headers
        ) as client:
            response = await client.head(url)
            if response.status_code in (405, 501):
                response = await client.get(url, headers={"Range": "bytes=0-0"})
    except Exception:  # noqa: BLE001 - network failures are data, not crashes
        return "error"

    status = response.status_code
    if 200 <= status < 400:
        return "ok"
    if status in (401, 403, 429):
        return "blocked"
    if status in (404, 410):
        return "invalid"
    return "invalid" if status < 500 else "error"


async def check_urls(
    urls: Sequence[str],
    sampler=None,
    sample: int = 20,
    concurrency: int = 8,
) -> MetricResult:
    """HTTP-check a deterministic sample of URLs."""
    unique = _dedupe([u for u in urls if u])
    picked = _even_sample(unique, sample)
    if not picked:
        return MetricResult(name="source_validity", value=None, notes="no_urls")

    sampler = sampler or http_sampler
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(url: str) -> tuple[str, str]:
        async with semaphore:
            try:
                return url, await sampler(url)
            except Exception:  # noqa: BLE001
                return url, "error"

    results = await asyncio.gather(*[one(url) for url in picked])
    counts = {status: 0 for status in VALID_STATUSES}
    for _, status in results:
        counts[status if status in counts else "error"] += 1

    checkable = counts["ok"] + counts["invalid"]
    return MetricResult(
        name="source_validity",
        value=_ratio(counts["ok"], checkable),
        numerator=counts["ok"],
        denominator=checkable,
        method="http",
        sampled=len(unique) > len(picked),
        notes=(
            f"sampled={len(picked)}/{len(unique)} blocked={counts['blocked']} "
            f"errors={counts['error']}"
        ),
    )
