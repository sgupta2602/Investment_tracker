"""Tests for the Income tab's event extraction and friendly relabeling."""
from datetime import datetime

from app.income import extract_income_events, income_totals
from app.parsing import Transaction


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


def test_journal_is_now_captured_as_an_income_event():
    """Regression: 'Journal' wasn't in INCOME_ACTIONS at all before, so
    these transactions were silently dropped -- invisible everywhere."""
    events = extract_income_events([_txn("Journal", -1000.0, "JOURNAL TO ...556")])
    assert len(events) == 1
    assert events[0]["amount"] == -1000.0


def test_moneylink_transfer_relabeled_withdrawal_when_negative():
    events = extract_income_events([_txn("MoneyLink Transfer", -2500.0)])
    assert events[0]["label"] == "Withdrawal"


def test_moneylink_transfer_relabeled_deposit_when_positive():
    events = extract_income_events([_txn("MoneyLink Transfer", 2500.0)])
    assert events[0]["label"] == "Deposit"


def test_journal_relabeled_to_another_account_when_negative():
    events = extract_income_events([_txn("Journal", -1000.0)])
    assert events[0]["label"] == "Journal Transfer (to Another Account)"


def test_journal_relabeled_from_another_account_when_positive():
    events = extract_income_events([_txn("Journal", 1000.0)])
    assert events[0]["label"] == "Journal Transfer (from Another Account)"


def test_dividend_and_fee_actions_pass_through_unchanged():
    events = extract_income_events(
        [_txn("Cash Dividend", 12.5), _txn("Qualified Dividend", 8.0), _txn("Foreign Tax Paid", -1.2)]
    )
    labels = {e["label"] for e in events}
    assert labels == {"Cash Dividend", "Qualified Dividend", "Foreign Tax Paid"}


def test_income_totals_grouped_and_summed_by_friendly_label_not_raw_action():
    events = extract_income_events(
        [
            _txn("MoneyLink Transfer", -2500.0),
            _txn("MoneyLink Transfer", -5000.0),  # same label, different raw txns -- should sum together
            _txn("Journal", -1000.0),
        ]
    )
    totals = income_totals(events)
    assert totals["Withdrawal"] == -7500.0
    assert totals["Journal Transfer (to Another Account)"] == -1000.0
    assert totals["Total"] == -8500.0
