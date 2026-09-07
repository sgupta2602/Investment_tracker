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
