"""T01: adaptive configuration keys must exist and be overridable."""

import pytest

from gpt_researcher.config import Config


def test_adaptive_defaults_are_disabled():
    cfg = Config()
    assert cfg.adaptive_enabled is False


def test_adaptive_default_values():
    cfg = Config()
    assert cfg.adaptive_max_rounds == 3
    assert cfg.adaptive_max_llm_calls == 120
    assert cfg.adaptive_max_tokens == 300000
    assert cfg.adaptive_max_seconds == 900
    assert cfg.adaptive_eval_mode == "light"
    assert cfg.adaptive_min_coverage == pytest.approx(0.80)
    assert cfg.adaptive_min_support_rate == pytest.approx(0.85)
    assert cfg.adaptive_no_progress_rounds == 2
    assert cfg.adaptive_state_dir == "outputs/adaptive"


def test_adaptive_env_override(monkeypatch):
    monkeypatch.setenv("ADAPTIVE_ENABLED", "true")
    monkeypatch.setenv("ADAPTIVE_MAX_ROUNDS", "5")
    monkeypatch.setenv("ADAPTIVE_MAX_COST_USD", "0.25")
    cfg = Config()
    assert cfg.adaptive_enabled is True
    assert cfg.adaptive_max_rounds == 5
    assert cfg.adaptive_max_cost_usd == pytest.approx(0.25)


def test_adaptive_env_override_does_not_leak_into_other_settings(monkeypatch):
    monkeypatch.setenv("ADAPTIVE_EVAL_MODE", "full")
    cfg = Config()
    assert cfg.adaptive_eval_mode == "full"
    # Existing settings keep their defaults.
    assert cfg.max_iterations == 3
    assert cfg.report_source == "web"
