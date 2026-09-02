"""Tests for CSV parsing: date handling, option-symbol decomposition, and
money-string cleanup. Uses a small synthetic fixture, not real account data.
"""
from datetime import datetime
from pathlib import Path

from app.parsing import (
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
