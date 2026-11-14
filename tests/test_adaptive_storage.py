"""T03: state persistence is atomic and round-trips."""

import json

import pytest

from gpt_researcher.adaptive import storage
from gpt_researcher.adaptive.state import Evidence, ResearchState, SubTask


def _state() -> ResearchState:
    return ResearchState(
        run_id="run_1",
        query="q",
        started_at="2026-11-14T00:00:00Z",
        plan=[SubTask(id="T1", query="q1", goal="g1")],
        evidence=[
            Evidence(
                id="E1",
                task_id="T1",
                url="https://example.com",
                title="t",
                snippet="s",
                query="q1",
                round=1,
                fetched_at="2026-11-14T00:00:00Z",
            )
        ],
    )


def test_save_and_load_round_trip(tmp_path):
    state = _state()
    path = storage.save_state(state, tmp_path / "run_1")
    assert path.exists()
    assert state.updated_at  # stamped on save

    loaded = storage.load_state(path)
    assert loaded.to_dict() == state.to_dict()


def test_load_accepts_directory(tmp_path):
    state = _state()
    storage.save_state(state, tmp_path / "run_1")
    loaded = storage.load_state(tmp_path / "run_1")
    assert loaded.query == "q"


def test_corrupted_state_raises_readable_error(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupted adaptive state"):
        storage.load_state(target)


def test_report_version_written(tmp_path):
    version = storage.save_report_version("# hello", 2, tmp_path)
    assert version.round == 2
    assert version.chars == len("# hello")
    assert version.sha256
    assert (tmp_path / "report_v2.md").read_text(encoding="utf-8") == "# hello"


def test_final_report_written(tmp_path):
    path = storage.save_final_report("# final", tmp_path)
    assert path.read_text(encoding="utf-8") == "# final"


def test_no_temp_file_left_when_replace_fails(tmp_path, monkeypatch):
    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(storage.os, "replace", boom)
    with pytest.raises(OSError):
        storage._atomic_write(tmp_path / "x.json", "{}")
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".x.json.")]
    assert leftovers == []


def test_state_json_is_valid_json(tmp_path):
    state = _state()
    path = storage.save_state(state, tmp_path)
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert parsed["run_id"] == "run_1"
    assert parsed["visited_urls"] == []
