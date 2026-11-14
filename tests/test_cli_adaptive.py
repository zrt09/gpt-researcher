"""T22: CLI wiring for --adaptive."""

import asyncio

import cli as cli_module


def test_adaptive_flag_is_parsed():
    args = cli_module.cli.parse_args(["q", "--adaptive"])
    assert args.adaptive is True
    assert args.report_type is None


def test_adaptive_rounds_override_is_parsed():
    args = cli_module.cli.parse_args(["q", "--adaptive", "--adaptive_rounds", "2"])
    assert args.adaptive_rounds == 2


def test_classic_mode_still_defaults_to_no_adaptive():
    args = cli_module.cli.parse_args(["q", "--report_type", "research_report"])
    assert args.adaptive is False
    assert args.report_type == "research_report"


def test_main_requires_report_type_in_classic_mode():
    args = cli_module.cli.parse_args(["q"])
    try:
        asyncio.run(cli_module.main(args))
    except SystemExit as exc:  # argparse error path
        assert exc.code != 0
    else:  # pragma: no cover - guard
        raise AssertionError("expected SystemExit when --report_type is missing")
