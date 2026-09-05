"""End-to-end API smoke test: upload the fixture CSV through the real
FastAPI app and check the dashboard renders with sane numbers. Uses an
isolated temp DB so it never touches real data."""
import importlib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

FIXTURE = Path(__file__).parent / "fixtures" / "sample_transactions.csv"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "test.db")
    import app.main as main_module

    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c


def test_upload_then_dashboard_shows_closed_trades(client):
    with open(FIXTURE, "rb") as f:
        resp = client.post(
            "/upload",
            files={"file": ("sample_transactions.csv", f, "text/csv")},
        )
    assert resp.status_code == 200  # TestClient follows the 303 redirect
    assert "ABCD" in resp.text
    # SCOPE (current pass): only options via Buy to Open / Sell to Close.
    # OLDCO is a plain share trade and must NOT show up in the trade log.
    assert "OLDCO" not in resp.text
    assert "Trade Log" in resp.text


def test_home_redirects_to_latest_upload_after_data_exists(client):
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("sample_transactions.csv", f, "text/csv")})
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


def test_home_shows_upload_form_when_no_data(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Upload your broker transaction CSV" in resp.text


def test_upload_page_always_reachable_even_after_data_exists(client):
    """Regression test: '/upload' must always render the form, not
    redirect back into a dashboard (that was the original bug -- the nav
    link pointed at '/' which just bounced you back to existing data)."""
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("sample_transactions.csv", f, "text/csv")})
    resp = client.get("/upload")
    assert resp.status_code == 200
    assert "Upload your broker transaction CSV" in resp.text


def test_overview_shows_performance_charts_after_upload(client):
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("sample_transactions.csv", f, "text/csv")})
    resp = client.get("/overview")
    assert resp.status_code == 200
    assert "cumulativeChart" in resp.text
    assert "termChart" in resp.text
    assert "periodChart" in resp.text


def test_home_redirects_to_overview_not_stale_dashboard(client):
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("sample_transactions.csv", f, "text/csv")})
    resp = client.get("/", follow_redirects=False)
    assert resp.headers["location"] == "/overview"


def test_delete_upload_removes_its_statement_and_trades(client):
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("sample_transactions.csv", f, "text/csv")})
    dashboard_before = client.get("/dashboard/1")
    assert "ABCD" in dashboard_before.text

    uploads = client.get("/upload")
    assert "sample_transactions.csv" in uploads.text

    # There's only one upload in a fresh DB, so its id is 1.
    resp = client.post("/delete_upload/1", follow_redirects=False)
    assert resp.status_code == 303
    # No uploads left -> bounced to the upload form, not a dead overview page.
    assert resp.headers["location"] == "/upload"

    home = client.get("/")
    assert "Upload your broker transaction CSV" in home.text


def test_delete_upload_unwinds_a_cross_upload_match(client):
    """The whole point of rebuilding (not doing a targeted delete) on
    every change: deleting the statement holding the OPENING leg of a
    trade must correctly turn its counterpart's CLOSING leg back into
    an unmatched close, not leave a orphaned/incorrect closed trade."""
    feb_csv = (
        '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
        '"02/10/2026","Buy to Open","XYZ 10/16/2026 90.00 C","CALL XYZ","1","$3.35","$0.66","-$335.66"\n'
    )
    june_csv = (
        '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
        '"06/15/2026","Sell to Close","XYZ 10/16/2026 90.00 C","CALL XYZ","1","$8.20","$0.66","$819.34"\n'
    )
    client.post("/upload", files={"file": ("feb.csv", feb_csv, "text/csv")})
    client.post("/upload", files={"file": ("june.csv", june_csv, "text/csv")})

    dashboard = client.get("/dashboard/2")  # june.csv is upload id 2
    assert "XYZ" in dashboard.text  # matched into one closed trade across both uploads

    # Upload 1 = feb.csv (the opening leg). Deleting it should unwind the match.
    client.post("/delete_upload/1")

    dashboard = client.get("/dashboard/2")
    assert dashboard.status_code == 200
    # XYZ's Sell to Close now has no opening leg left -- it belongs in
    # Needs Review as an unmatched close, not a phantom closed trade.
    assert "Needs Review" in dashboard.text
