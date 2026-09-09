"""Tests for the annotation layer (Notes/Comments, Recommended By, Reason).

The critical property under test: closed_trades gets fully DELETE+
reinserted on every upload (replace_closed_trades rebuilds cumulative
columns from scratch), so annotations MUST live in a separate table keyed
by a stable natural key -- not by closed_trades.id, which is a fresh
autoincrement value every rebuild.
"""
from datetime import datetime

import pytest

from app import db, repository as repo


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def _sample_trade(**overrides) -> dict:
    base = {
        "account": "TEST123",
        "ticker": "ABCD",
        "strike_price": 50.0,
        "expiration": datetime(2026, 2, 6),
        "buy_date": datetime(2026, 1, 2),
        "sell_date": datetime(2026, 1, 10),
        "quantity": 200.0,
    }
    base.update(overrides)
    return base


def test_trade_key_is_deterministic():
    t = _sample_trade()
    assert repo.trade_key(t) == repo.trade_key(_sample_trade())


def test_trade_key_differs_for_different_trades():
    assert repo.trade_key(_sample_trade()) != repo.trade_key(_sample_trade(ticker="WXYZ"))
    assert repo.trade_key(_sample_trade()) != repo.trade_key(_sample_trade(quantity=100.0))


def test_save_and_load_annotation_roundtrip():
    key = repo.trade_key(_sample_trade())
    repo.save_trade_annotation_field(key, "recommended_by", "Priya")
    repo.save_trade_annotation_field(key, "reason", "Earnings beat expected")

    all_annotations = repo.load_all_annotations()
    assert all_annotations[key]["recommended_by"] == "Priya"
    assert all_annotations[key]["reason"] == "Earnings beat expected"
    # notes was never touched -- should not have been clobbered by the
    # other two field-scoped writes (this is the whole point of the
    # ON CONFLICT DO UPDATE SET <single column> approach).
    assert not all_annotations[key]["notes"]


def test_save_trade_annotation_field_rejects_unknown_field():
    with pytest.raises(ValueError):
        repo.save_trade_annotation_field("some-key", "not_a_real_field", "x")


def test_attach_annotations_merges_saved_values_into_trade_dicts():
    trade = _sample_trade()
    key = repo.trade_key(trade)
    repo.save_trade_annotation_field(key, "recommended_by", "Amit")

    enriched = repo._attach_annotations([trade])
    assert enriched[0]["recommended_by"] == "Amit"
    assert enriched[0]["reason"] == ""  # untouched fields default to empty string, not None
    assert enriched[0]["trade_key"] == key


def test_annotation_survives_a_simulated_closed_trades_rebuild():
    """The whole reason this table is separate: simulate the exact
    thing replace_closed_trades() does (DELETE + reinsert closed_trades)
    and confirm the annotation -- which lives in a different table
    entirely -- is untouched and still attaches correctly afterward."""
    trade = _sample_trade()
    key = repo.trade_key(trade)
    repo.save_trade_annotation_field(key, "recommended_by", "Cousin Raj")

    # Simulate a full statement re-upload's rebuild: closed_trades wiped
    # and reinserted with the SAME underlying trade (same transactions
    # in, same FIFO match out -- as would happen on a real re-upload).
    with db.get_conn() as conn:
        conn.execute("DELETE FROM closed_trades")
    repo.replace_closed_trades(_enriched_row(trade))

    reloaded = repo.load_all_closed_trades()
    assert len(reloaded) == 1
    assert reloaded[0]["recommended_by"] == "Cousin Raj"


def _enriched_row(trade: dict) -> list[dict]:
    """Minimal enriched-trade dict shape replace_closed_trades() expects
    (the fields calc.enrich_trades() would normally add)."""
    return [
        {
            **trade,
            "upload_id": 1,
            "equity_type": "Options",
            "sell_price": 3.0,
            "cost_price": 1.0,
            "break_even": 1.0,
            "realized_value": 600.0,
            "cost_basis": 200.0,
            "cumulative_investment": 200.0,
            "hold_period_months": 0.27,
            "gain_loss": 400.0,
            "pct_gain_loss": 2.0,
            "gain_per_month": 7.4,
            "gain_type": "Short",
            "cumulative_gain": 400.0,
            "cumulative_gain_pct": 2.0,
            "estimated_tax": 148.0,
            "is_adjusted": False,
        }
    ]


# --- Upload period labels / account grouping -------------------------------


def _txn(**overrides):
    from app.parsing import Transaction

    base = dict(
        date=datetime(2026, 1, 5),
        action="Buy to Open",
        symbol="ABCD 01/16/2026 50.00 C",
        description="",
        quantity=1.0,
        price=1.0,
        fees=0.0,
        amount=-100.0,
    )
    base.update(overrides)
    return Transaction(**base)


def test_format_period_label_single_day():
    label = repo._format_period_label(datetime(2026, 1, 5), datetime(2026, 1, 5), "f.csv")
    assert label == "Jan 05, 2026"


def test_format_period_label_same_month():
    label = repo._format_period_label(datetime(2026, 1, 1), datetime(2026, 1, 31), "f.csv")
    assert label == "Jan 01\u201331, 2026"


def test_format_period_label_same_year_different_months():
    label = repo._format_period_label(datetime(2026, 1, 15), datetime(2026, 2, 20), "f.csv")
    assert label == "Jan 15 \u2013 Feb 20, 2026"


def test_format_period_label_spans_years():
    label = repo._format_period_label(datetime(2025, 12, 15), datetime(2026, 1, 10), "f.csv")
    assert label == "Dec 15, 2025 \u2013 Jan 10, 2026"


def test_format_period_label_falls_back_to_filename_with_no_dates():
    assert repo._format_period_label(None, None, "empty_statement.csv") == "empty_statement.csv"


def test_list_uploads_computes_period_label_from_actual_transaction_dates():
    """Not the filename, not the upload timestamp -- the real MIN/MAX
    transaction date contained in that upload."""
    upload_id = repo.create_upload("Jan_Statement.csv", "XXX939")
    repo.save_raw_transactions(
        upload_id,
        [_txn(date=datetime(2026, 1, 3)), _txn(date=datetime(2026, 1, 28))],
    )

    [upload] = repo.list_uploads()
    assert upload["period_label"] == "Jan 03\u201328, 2026"


def test_list_uploads_sorted_by_period_not_upload_order():
    """Uploading February's statement before January's must still show
    January first -- 'Viewing period' is about the covered date range,
    not the order you happened to upload them in."""
    feb_id = repo.create_upload("Feb.csv", "XXX939")
    repo.save_raw_transactions(feb_id, [_txn(date=datetime(2026, 2, 1))])
    jan_id = repo.create_upload("Jan.csv", "XXX939")
    repo.save_raw_transactions(jan_id, [_txn(date=datetime(2026, 1, 1))])

    uploads = repo.list_uploads()
    assert [u["id"] for u in uploads] == [feb_id, jan_id]  # newest period first


def test_group_uploads_by_account_preserves_relative_order_per_group():
    uploads = [
        {"id": 1, "account": "XXX111", "period_label": "Feb 2026"},
        {"id": 2, "account": "XXX939", "period_label": "Feb 2026"},
        {"id": 3, "account": "XXX111", "period_label": "Jan 2026"},
    ]
    grouped = repo.group_uploads_by_account(uploads)
    by_account = {g["account"]: [u["id"] for u in g["uploads"]] for g in grouped}

    assert by_account == {"XXX111": [1, 3], "XXX939": [2]}
    # Accounts themselves come back alphabetically sorted.
    assert [g["account"] for g in grouped] == ["XXX111", "XXX939"]
