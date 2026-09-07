"""Tests for CSV parsing: date handling, option-symbol decomposition, and
money-string cleanup. Uses a small synthetic fixture, not real account data.
"""
from datetime import datetime
from pathlib import Path

from app.parsing import (
    _parse_money,
    extract_account_label,
    parse_option_symbol,
    parse_transactions_csv,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_transactions.csv"


def test_parses_all_rows():
    txns = parse_transactions_csv(FIXTURE)
    assert len(txns) == 8


def test_option_symbol_parsed_into_parts():
    txns = parse_transactions_csv(FIXTURE)
    abcd_open = next(t for t in txns if t.symbol.startswith("ABCD"))
    assert abcd_open.is_option
    assert abcd_open.underlying == "ABCD"
    assert abcd_open.strike == 50.00
    assert abcd_open.right == "C"
    assert abcd_open.expiration == datetime(2026, 2, 6)


def test_plain_stock_symbol_is_not_an_option():
    txns = parse_transactions_csv(FIXTURE)
    oldco = next(t for t in txns if t.symbol == "OLDCO")
    assert not oldco.is_option
    assert oldco.display_ticker == "OLDCO"


def test_expired_uses_as_of_date_not_settlement_date():
    txns = parse_transactions_csv(FIXTURE)
    expired = next(t for t in txns if t.action == "Expired")
    assert expired.date == datetime(2026, 1, 16)


def test_money_and_fee_parsing_handles_dollar_signs_and_negatives():
    txns = parse_transactions_csv(FIXTURE)
    sell = next(t for t in txns if t.symbol == "ABCD 02/06/2026 50.00 C" and t.action == "Sell to Close")
    assert sell.price == 3.00
    assert sell.fees == 1.32
    assert sell.amount == 598.68


def test_extract_account_label_from_filename():
    assert extract_account_label("Joint_Tenant_XXX939_Transactions_20260830.csv") == "XXX939"
    assert extract_account_label("no_account_here.csv") is None


def test_parse_option_symbol_rejects_non_option_strings():
    assert parse_option_symbol("OLDCO") == {}


def test_parse_money_handles_dash_negative():
    assert _parse_money("-$292.66") == -292.66


def test_parse_money_handles_accounting_parens_negative():
    """Regression test: real broker export used '($292.66)' (accounting/
    parentheses notation) instead of '-$292.66' -- this crashed the parser
    with a ValueError before the fix, since '(292.66)' isn't a valid float
    once $ and , are stripped but the parens are left in place."""
    assert _parse_money("($292.66)") == -292.66


def test_parse_money_handles_accounting_parens_with_thousands_separator():
    assert _parse_money("($1,455.00)") == -1455.00


def test_parse_money_handles_positive_with_thousands_separator():
    assert _parse_money("$2,096.63") == 2096.63


def test_parse_money_handles_blank():
    assert _parse_money("") == 0.0
    assert _parse_money(None) == 0.0
