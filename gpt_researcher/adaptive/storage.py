"""Persistence helpers for the adaptive research loop.

Writes are atomic (temp file + ``os.replace``) so a crash mid-write can never
leave a half-written ``state.json`` that would break the next load.

Layout (see docs/改造方案/07_State数据结构设计.md):

    <state_dir>/<run_id>/
        state.json
        report_v1.md
        report_v2.md
        final_report.md
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .state import ReportVersion, ResearchState

STATE_FILENAME = "state.json"


def utc_now() -> str:
    """ISO-8601 UTC timestamp used across the adaptive package."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_run_id(query: str) -> str:
    """Stable-ish id: timestamp + short hash of the query."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:8]
    return f"{stamp}_{digest}"


def run_dir(base_dir: str | Path, run_id: str) -> Path:
    path = Path(base_dir) / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # Never leave the temp file behind on failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_state(state: ResearchState, directory: str | Path) -> Path:
    """Persist the state atomically and stamp ``updated_at``."""
    state.updated_at = utc_now()
    target = Path(directory) / STATE_FILENAME
    _atomic_write(target, state.to_json())
    return target


def load_state(path: str | Path) -> ResearchState:
    """Read a previously saved state file."""
    target = Path(path)
    if target.is_dir():
        target = target / STATE_FILENAME
    text = target.read_text(encoding="utf-8")
    try:
        return ResearchState.from_json(text)
    except Exception as exc:  # noqa: BLE001 - surface a readable error
        raise ValueError(f"corrupted adaptive state at {target}: {exc}") from exc


def save_report_version(
    text: str,
    round_no: int,
    directory: str | Path,
    decision: str | None = None,
) -> ReportVersion:
    """Write ``report_vN.md`` and return its metadata record."""
    target = Path(directory) / f"report_v{round_no}.md"
    _atomic_write(target, text)
    return ReportVersion(
        round=round_no,
        path=str(target),
        chars=len(text),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        decision=decision,
    )


def save_final_report(text: str, directory: str | Path) -> Path:
    target = Path(directory) / "final_report.md"
    _atomic_write(target, text)
    return target
