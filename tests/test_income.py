"""Tests for cash-event extraction and friendly relabeling: income
(dividends/fees) and transfers (MoneyLink/Journal) share the same
extraction step (extract_cash_events) but are split into two separate
tabs at display time (filter_income_events / filter_transfer_events).
"""
from datetime import datetime

from app.income import event_totals, extract_cash_events, filter_income_events
from app.parsing import Transaction
from app.transfers import filter_transfer_events


def _txn(action: str, amount: float, description: str = "") -> Transaction:
    return Transaction(
        date=datetime(2026, 1, 1),
        action=action,
        symbol="",
        description=description,
        quantity=0.0,
        price=0.0,
        fees=0.0,
        amount=amount,
    )


def test_journal_is_captured_as_a_cash_event():
    """Regression: 'Journal' wasn't in any action set at all before, so
    these transactions were silently dropped -- invisible everywhere."""
    events = extract_cash_events([_txn("Journal", -1000.0, "JOURNAL TO ...556")])
    assert len(events) == 1
    assert events[0]["amount"] == -1000.0


def test_moneylink_transfer_relabeled_withdrawal_when_negative():
    events = extract_cash_events([_txn("MoneyLink Transfer", -2500.0)])
    assert events[0]["label"] == "Withdrawal"


def test_moneylink_transfer_relabeled_deposit_when_positive():
    events = extract_cash_events([_txn("MoneyLink Transfer", 2500.0)])
    assert events[0]["label"] == "Deposit"


def test_journal_relabeled_to_another_account_when_negative():
    events = extract_cash_events([_txn("Journal", -1000.0)])
    assert events[0]["label"] == "Journal Transfer (to Another Account)"


def test_journal_relabeled_from_another_account_when_positive():
    events = extract_cash_events([_txn("Journal", 1000.0)])
    assert events[0]["label"] == "Journal Transfer (from Another Account)"


def test_dividend_and_fee_actions_pass_through_unchanged():
    events = extract_cash_events(
        [_txn("Cash Dividend", 12.5), _txn("Qualified Dividend", 8.0), _txn("Foreign Tax Paid", -1.2)]
    )
    labels = {e["label"] for e in events}
    assert labels == {"Cash Dividend", "Qualified Dividend", "Foreign Tax Paid"}


def test_event_totals_grouped_and_summed_by_friendly_label_not_raw_action():
    events = extract_cash_events(
        [
            _txn("MoneyLink Transfer", -2500.0),
            _txn("MoneyLink Transfer", -5000.0),  # same label, different raw txns -- should sum together
            _txn("Journal", -1000.0),
        ]
    )
    totals = event_totals(events)
    assert totals["Withdrawal"] == -7500.0
    assert totals["Journal Transfer (to Another Account)"] == -1000.0
    assert totals["Total"] == -8500.0


def test_filter_income_events_excludes_transfers():
    """The whole point of the Income/Transfers split: Withdrawal and
    Journal Transfer must NOT show up on the Income tab."""
    events = extract_cash_events(
        [
            _txn("Cash Dividend", 12.5),
            _txn("MoneyLink Transfer", -2500.0),
            _txn("Journal", -1000.0),
        ]
    )
    income_only = filter_income_events(events)
    assert len(income_only) == 1
    assert income_only[0]["label"] == "Cash Dividend"


def test_filter_transfer_events_excludes_income():
    """And the inverse: dividends/fees must NOT show up on Transfers."""
    events = extract_cash_events(
        [
            _txn("Cash Dividend", 12.5),
            _txn("MoneyLink Transfer", -2500.0),
            _txn("Journal", -1000.0),
        ]
    )
    transfers_only = filter_transfer_events(events)
    labels = {e["label"] for e in transfers_only}
    assert len(transfers_only) == 2
    assert labels == {"Withdrawal", "Journal Transfer (to Another Account)"}


def test_income_and_transfer_filters_are_fully_disjoint():
    """Every cash event must land in exactly one tab, never both, never neither."""
    events = extract_cash_events(
        [
            _txn("Cash Dividend", 12.5),
            _txn("Qualified Dividend", 8.0),
            _txn("ADR Mgmt Fee", -1.0),
            _txn("Foreign Tax Paid", -1.2),
            _txn("MoneyLink Transfer", -2500.0),
            _txn("Journal", -1000.0),
        ]
    )
    income_only = filter_income_events(events)
    transfers_only = filter_transfer_events(events)
    assert len(income_only) + len(transfers_only) == len(events)
    assert set(id(e) for e in income_only).isdisjoint(set(id(e) for e in transfers_only))
