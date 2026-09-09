"""Tests for transaction-level deduplication on upload.

Why this exists: some broker export styles re-include the whole year
every time you download (a "Jan 1 to today" CSV), so re-uploading a
superset statement in a later month would otherwise re-insert every
already-known transaction, silently doubling every total. See
repository.transaction_key() / load_all_transaction_keys().
"""
from datetime import datetime

import pytest

from app import db, repository as repo
from app.parsing import Transaction

FEB_CSV = (
    '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
    '"02/10/2026","Buy to Open","XYZ 10/16/2026 90.00 C","CALL XYZ","1","$3.35","$0.66","-$335.66"\n'
    '"02/15/2026","Sell to Close","XYZ 10/16/2026 90.00 C","CALL XYZ","1","$8.20","$0.66","$819.34"\n'
)
# A "Jan-Nov" style re-export: same two Feb rows verbatim, plus one new June row.
FEB_PLUS_JUNE_CSV = (
    '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
    '"02/10/2026","Buy to Open","XYZ 10/16/2026 90.00 C","CALL XYZ","1","$3.35","$0.66","-$335.66"\n'
    '"02/15/2026","Sell to Close","XYZ 10/16/2026 90.00 C","CALL XYZ","1","$8.20","$0.66","$819.34"\n'
    '"06/01/2026","Buy to Open","QRS 08/01/2026 20.00 C","CALL QRS","1","$1.00","$0.10","-$100.10"\n'
    '"06/15/2026","Sell to Close","QRS 08/01/2026 20.00 C","CALL QRS","1","$2.00","$0.10","$199.90"\n'
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def _txn(**overrides) -> Transaction:
    base = dict(
        date=datetime(2026, 2, 10),
        action="Buy to Open",
        symbol="XYZ 10/16/2026 90.00 C",
        description="CALL XYZ",
        quantity=1,
        price=3.35,
        fees=0.66,
        amount=-335.66,
        account="ACCT_A",
    )
    base.update(overrides)
    return Transaction(**base)


def test_transaction_key_is_deterministic():
    assert repo.transaction_key(_txn()) == repo.transaction_key(_txn())


def test_transaction_key_differs_by_account():
    """The identical row content in two different accounts must NOT be
    treated as a duplicate of each other."""
    assert repo.transaction_key(_txn(account="ACCT_A")) != repo.transaction_key(
        _txn(account="ACCT_B")
    )


def test_transaction_key_differs_when_any_field_changes():
    baseline = repo.transaction_key(_txn())
    assert repo.transaction_key(_txn(quantity=2)) != baseline
    assert repo.transaction_key(_txn(price=4.0)) != baseline
    assert repo.transaction_key(_txn(action="Sell to Close")) != baseline


def test_load_all_transaction_keys_reflects_saved_transactions():
    upload_id = repo.create_upload("test.csv", "ACCT_A")
    txn = _txn()
    txn.upload_id = upload_id
    repo.save_raw_transactions(upload_id, [txn])

    keys = repo.load_all_transaction_keys()
    assert repo.transaction_key(txn) in keys
