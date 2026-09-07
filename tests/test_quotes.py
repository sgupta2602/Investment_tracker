"""Tests for the per-login trading-wisdom quote banner."""
from app.quotes import TRADING_QUOTES, random_quote


def test_random_quote_returns_one_of_the_list():
    for _ in range(20):
        assert random_quote() in TRADING_QUOTES


def test_quote_list_has_no_duplicates():
    assert len(TRADING_QUOTES) == len(set(TRADING_QUOTES))


def test_quote_list_is_a_reasonable_size():
    """Not a hard requirement, just a sanity check that this doesn't
    degrade into a list of 1-2 quotes that'd feel repetitive."""
    assert len(TRADING_QUOTES) >= 10
