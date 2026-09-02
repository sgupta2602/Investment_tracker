"""FastAPI app: upload a broker CSV, get the trade log + monthly
performance + gains/losses/tax + income breakdown that used to be a
manual Excel chore."""
from __future__ import annotations

import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import repository as repo
from app.db import init_db
from app.income import extract_income_events, income_totals
from app.calc import enrich_trades
from app.matching import match_transactions
from app.parsing import extract_account_label, parse_transactions_csv
from app.summary import gains_losses_by_term, monthly_performance

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Investment Tracker", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["usd"] = lambda v: ("-$" if v < 0 else "$") + f"{abs(v):,.2f}"
templates.env.filters["pct"] = lambda v: f"{v * 100:,.2f}%"


@app.get("/")
def home(request: Request):
    uploads = repo.list_uploads()
    if not uploads:
        return templates.TemplateResponse(request, "upload.html", {"uploads": uploads})
    return RedirectResponse(url=f"/dashboard/{uploads[0]['id']}")


@app.post("/upload")
async def upload_csv(file: UploadFile = File(...)):
    account = extract_account_label(file.filename)

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        transactions = parse_transactions_csv(tmp_path, account=account)
    finally:
        tmp_path.unlink(missing_ok=True)

    upload_id = repo.create_upload(file.filename, account)
    for t in transactions:
        t.upload_id = upload_id
    repo.save_raw_transactions(upload_id, transactions)

    income_events = extract_income_events(transactions)
    repo.save_income_events(upload_id, income_events)

    _rebuild_closed_trades()

    return RedirectResponse(url=f"/dashboard/{upload_id}", status_code=303)


def _rebuild_closed_trades() -> None:
    """Re-runs matching + formula enrichment across ALL history. Cheap
    for personal data volumes, and guarantees cumulative columns and
    cross-month lot matching stay correct no matter the upload order."""
    all_transactions = repo.load_all_transactions()
    match_result = match_transactions(all_transactions)

    raw_trades = sorted(match_result.closed_trades, key=lambda t: t.sell_date)
    trade_dicts = [
        {
            "upload_id": t.upload_id,
            "account": t.account,
            "ticker": t.ticker,
            "equity_type": t.equity_type,
            "quantity": t.quantity,
            "buy_date": t.buy_date,
            "sell_date": t.sell_date,
            "cost_price": t.cost_price,
            "sell_price": t.sell_price,
            "strike_price": t.strike_price,
            "expiration": t.expiration,
        }
        for t in raw_trades
    ]
    enriched = enrich_trades(trade_dicts)
    repo.replace_closed_trades(enriched)


@app.get("/dashboard/{upload_id}")
def dashboard(request: Request, upload_id: int):
    uploads = repo.list_uploads()
    all_trades = repo.load_all_closed_trades()
    month_trades = repo.load_closed_trades_for_upload(upload_id)
    income_events = repo.load_income_events_for_upload(upload_id)

    # Recomputed fresh (cheap at personal data volumes) so "needs review"
    # always reflects current book state, not a stale snapshot.
    match_result = match_transactions(repo.load_all_transactions())

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "uploads": uploads,
            "current_upload_id": upload_id,
            "all_trades": all_trades,
            "month_trades": month_trades,
            "performance": monthly_performance(month_trades),
            "term_breakdown": gains_losses_by_term(month_trades),
            "income_events": income_events,
            "income_totals": income_totals(income_events),
            "unmatched_closes": match_result.unmatched_closes,
            "open_positions": match_result.open_positions,
        },
    )
