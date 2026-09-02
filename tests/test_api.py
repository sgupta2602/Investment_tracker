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
    assert "OLDCO" in resp.text
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
