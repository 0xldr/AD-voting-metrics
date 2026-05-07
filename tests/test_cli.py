"""Tests for cli argparse callbacks (other than parse_month) which lives
in test_period.py."""

import argparse

import pytest

from ad_voting_metrics.cli import parse_cache_hours


def test_parse_cache_hours():
    assert parse_cache_hours("0") == 0


def test_parse_cache_hours_accepts_positive_integers():
    assert parse_cache_hours("24") == 24


def test_parse_cache_hours_rejects_negative():
    with pytest.raises(argparse.ArgumentTypeError, match="negative"):
        parse_cache_hours("-1")


def test_parse_cache_hours_rejects_non_integer():
    with pytest.raises(argparse.ArgumentTypeError, match="not an integer"):
        parse_cache_hours("twelve")


def test_parse_cache_hours_rejects_float():
    with pytest.raises(argparse.ArgumentTypeError, match="not an integer"):
        parse_cache_hours("12.5")
