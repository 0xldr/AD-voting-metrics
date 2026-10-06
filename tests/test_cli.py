"""Tests for cli: parse_month, build_arg_parser, check_period_has_ended, rpc_url_from_env, logging, and main."""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from web3 import Web3

from ad_voting_metrics.cli import (
    RedactingFormatter,
    build_arg_parser,
    check_period_has_ended,
    configure_logging,
    main,
    parse_month,
    rpc_url_from_env,
)
from ad_voting_metrics.period import MonthPeriod


def test_parse_month_returns_month_period():
    assert parse_month("April 2026") == MonthPeriod(2026, 4)


def test_parse_month_rejects_unparseable_input_naming_it():
    with pytest.raises(argparse.ArgumentTypeError, match="not a date"):
        parse_month("not a date")


def test_check_period_has_ended_accepts_the_day_after_the_period():
    check_period_has_ended(MonthPeriod(2026, 4), today=date(2026, 5, 1))


@pytest.mark.parametrize("today", [date(2026, 4, 30), date(2026, 4, 15)], ids=["last day", "mid-period"])
def test_check_period_has_ended_rejects_a_period_still_in_progress(today):
    with pytest.raises(SystemExit, match="has not yet ended"):
        check_period_has_ended(MonthPeriod(2026, 4), today=today)


def test_parser_defaults():
    args = build_arg_parser().parse_args(["--month", "April 2026"])

    assert args.month == MonthPeriod(2026, 4)
    assert args.rebuild is False
    assert args.verbose is False
    assert args.roster == Path("delegates.yaml")
    assert args.output_dir == Path("output_data")


def test_parser_overrides():
    argv = ["--month", "April 2026", "--rebuild", "-v", "--roster", "r.yaml", "--output-dir", "/tmp/out"]

    args = build_arg_parser().parse_args(argv)

    assert args.rebuild is True
    assert args.verbose is True
    assert args.roster == Path("r.yaml")
    assert args.output_dir == Path("/tmp/out")


def test_parser_requires_month():
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args([])


def test_rpc_url_from_env_exits_when_unset(monkeypatch):
    monkeypatch.delenv("SKY_RPC_URL", raising=False)
    with pytest.raises(SystemExit, match="SKY_RPC_URL"):
        rpc_url_from_env()


def test_rpc_url_from_env_returns_the_value(monkeypatch):
    monkeypatch.setenv("SKY_RPC_URL", "http://localhost:8545")
    assert rpc_url_from_env() == "http://localhost:8545"


def test_redacting_formatter_blanks_secrets_in_messages_and_tracebacks():
    formatter = RedactingFormatter("%(message)s", "%Y", ["https://rpc.example/v2/SECRET", "/v2/SECRET", "/"])
    try:
        raise ConnectionError("Max retries exceeded with url: /v2/SECRET")  # noqa: TRY301
    except ConnectionError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed for /v2/SECRET", None, sys.exc_info())

    out = formatter.format(record)

    assert "SECRET" not in out
    assert out.count("<redacted>") >= 2
    assert "Traceback" in out


def test_configure_logging_replaces_its_own_handler_and_sets_verbosity():
    root = logging.getLogger()
    before = [h for h in root.handlers if not isinstance(h.formatter, RedactingFormatter)]

    configure_logging(verbose=True, secrets=["s3cret"])
    configure_logging(verbose=False, secrets=["s3cret"])

    redacting = [h for h in root.handlers if isinstance(h.formatter, RedactingFormatter)]
    assert len(redacting) == 1
    assert [h for h in root.handlers if not isinstance(h.formatter, RedactingFormatter)] == before
    assert logging.getLogger("ad_voting_metrics").level == logging.NOTSET
    root.removeHandler(redacting[0])


def test_main_runs_pipeline(monkeypatch):
    """main(['--month', ...]) runs the pipeline with the parsed period, paths, and a Web3 client."""
    monkeypatch.setattr("ad_voting_metrics.cli.check_period_has_ended", MagicMock())
    monkeypatch.setenv("SKY_RPC_URL", "http://localhost:8545")

    with patch("ad_voting_metrics.cli.run") as run_mock:
        main(["--month", "2026-04"])

    run_mock.assert_called_once()
    args, kwargs = run_mock.call_args
    assert args == (MonthPeriod(year=2026, month=4),)
    assert kwargs["rebuild"] is False
    assert kwargs["roster_path"] == Path("delegates.yaml")
    assert kwargs["output_dir"] == Path("output_data")
    assert isinstance(kwargs["w3"], Web3)


def test_main_reports_a_failed_run_with_the_rpc_key_redacted(monkeypatch, capsys):
    monkeypatch.setattr("ad_voting_metrics.cli.check_period_has_ended", MagicMock())
    monkeypatch.setenv("SKY_RPC_URL", "https://rpc.example/v2/SECRETKEY123")

    with (
        patch("ad_voting_metrics.cli.run", side_effect=RuntimeError("Max retries exceeded with url: /v2/SECRETKEY123")),
        pytest.raises(SystemExit) as exit_info,
    ):
        main(["--month", "2026-04"])

    err = capsys.readouterr().err
    assert exit_info.value.code == 1
    assert "Run failed" in err
    assert "SECRETKEY123" not in err
    assert "<redacted>" in err
