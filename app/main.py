"""FastAPI app: upload a broker CSV, get the trade log + monthly
performance + gains/losses/tax + income breakdown that used to be a
manual Excel chore."""
from __future__ import annotations

import json
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
from app.calc import LONG_TERM_RATE, LONG_TERM_THRESHOLD_DAYS, SHORT_TERM_RATE, enrich_trades
from app.matching import OPTION_MULTIPLIER, match_transactions
from app.parsing import extract_account_label, parse_transactions_csv
from app.summary import gains_losses_by_term, monthly_performance, performance_by_upload

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Investment Tracker", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["usd"] = lambda v: ("-$" if v < 0 else "$") + f"{abs(v):,.2f}"
templates.env.filters["pct"] = lambda v: f"{v * 100:,.2f}%"
templates.env.filters["tojson"] = json.dumps


@app.get("/")
def home(request: Request):
    uploads = repo.list_uploads()
    if not uploads:
        return templates.TemplateResponse(request, "upload.html", {"uploads": uploads})
    return RedirectResponse(url="/overview")


@app.get("/upload")
def upload_form(request: Request):
    """Always shows the upload form -- distinct from '/' so that once
    data exists, there's still a real way back here to add another
    month's statement (previously this was a dead loop)."""
    return templates.TemplateResponse(request, "upload.html", {"uploads": repo.list_uploads()})


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


@app.get("/overview")
def overview(request: Request):
    uploads = repo.list_uploads()
    if not uploads:
        return RedirectResponse(url="/upload")

    all_trades = repo.load_all_closed_trades()
    period_series = performance_by_upload(all_trades, uploads)
    cumulative_points = [
        {"date": t["sell_date"].strftime("%m/%d/%Y"), "value": round(t["cumulative_gain"], 2)}
        for t in all_trades
    ]

    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "uploads": uploads,
            "performance": monthly_performance(all_trades),
            "term_breakdown": gains_losses_by_term(all_trades),
            "period_series": period_series,
            "cumulative_points": cumulative_points,
            "trade_count": len(all_trades),
        },
    )


@app.get("/glossary")
def glossary(request: Request):
    """Static reference page explaining every formula/rule the engine
    applies. Rates/thresholds are read from the actual constants in
    calc.py / matching.py -- never hardcoded here -- so this page can
    never drift out of sync with what the app actually computes."""
    return templates.TemplateResponse(
        request,
        "glossary.html",
        {
            "long_term_rate": LONG_TERM_RATE,
            "short_term_rate": SHORT_TERM_RATE,
            "long_term_threshold_days": LONG_TERM_THRESHOLD_DAYS,
            "option_multiplier": OPTION_MULTIPLIER,
        },
    )
