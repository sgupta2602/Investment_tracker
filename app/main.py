"""FastAPI app: upload a broker CSV, get the trade log + monthly
performance + gains/losses/tax + income breakdown that used to be a
manual Excel chore."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import repository as repo
from app.db import init_db
from app.income import extract_income_events, income_totals
from app.calc import LONG_TERM_RATE, LONG_TERM_THRESHOLD_DAYS, SHORT_TERM_RATE, enrich_trades
from app.matching import OPTION_MULTIPLIER, match_transactions
from app.parsing import extract_account_label, parse_transactions_csv
from app.summary import gains_losses_by_term, monthly_performance, performance_by_upload

BASE_DIR = Path(__file__).resolve().parent

# All three are overridable via env vars (e.g. on Render) without a code
# change, but default to real values so this works locally with zero setup.
LOGIN_EMAIL = os.environ.get("LOGIN_EMAIL", "drskumar1164@gmail.com")
LOGIN_PASSWORD = os.environ.get("LOGIN_PASSWORD", "Sandy@1164")
# Falls back to a freshly-generated secret if unset -- fine for local use,
# but means sessions won't survive a server restart. Set SESSION_SECRET_KEY
# on Render so logins persist across deploys/restarts there.
SESSION_SECRET = os.environ.get("SESSION_SECRET_KEY", secrets.token_hex(32))

# Diagnostic only, printed once at startup -- no secret VALUES logged, just
# where each one came from and its length, so an env var silently
# overriding the expected default (or a stray whitespace char in one set
# via the Render dashboard) is visible in the Logs tab immediately, instead
# of needing to guess from failed login attempts.
print(
    f"[startup] LOGIN_EMAIL source={'env' if 'LOGIN_EMAIL' in os.environ else 'default'} "
    f"len={len(LOGIN_EMAIL)} | LOGIN_PASSWORD source="
    f"{'env' if 'LOGIN_PASSWORD' in os.environ else 'default'} len={len(LOGIN_PASSWORD)}",
    flush=True,
)

_PUBLIC_PATHS = {"/login"}


class RequireLoginMiddleware(BaseHTTPMiddleware):
    """Gates every route except /login behind a session flag set at login
    time. Must be added BEFORE SessionMiddleware (see bottom of this file --
    Starlette runs middleware in reverse-add order, so SessionMiddleware
    needs to be outermost to populate request.session first)."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path not in _PUBLIC_PATHS and not request.session.get("logged_in"):
            return RedirectResponse(url="/login")
        return await call_next(request)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Investment Tracker", lifespan=lifespan)
app.add_middleware(RequireLoginMiddleware)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["usd"] = lambda v: ("-$" if v < 0 else "$") + f"{abs(v):,.2f}"
templates.env.filters["pct"] = lambda v: f"{v * 100:,.2f}%"
templates.env.filters["tojson"] = json.dumps


@app.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    email_ok = secrets.compare_digest(email.strip().lower(), LOGIN_EMAIL.lower())
    password_ok = secrets.compare_digest(password, LOGIN_PASSWORD)
    if not (email_ok and password_ok):
        # Diagnostic only -- no secret VALUES logged, just whether each
        # field matched and its length, so a mismatch (typo, autocorrect,
        # or an env var silently overriding the expected credentials) is
        # visible in Render's Logs tab instead of guessing blind.
        print(
            f"[login] rejected -- email_ok={email_ok} password_ok={password_ok} "
            f"received_email_len={len(email)} expected_email_len={len(LOGIN_EMAIL)} "
            f"received_password_len={len(password)} expected_password_len={len(LOGIN_PASSWORD)}",
            flush=True,
        )
        return templates.TemplateResponse(
            request, "login.html", {"error": "Incorrect email or password."}, status_code=401
        )
    request.session["logged_in"] = True
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


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


@app.post("/delete_upload/{upload_id}")
def delete_upload(upload_id: int):
    """Deletes a statement and re-derives closed_trades from whatever
    uploads remain -- see repository.delete_upload() for why the rebuild
    (not a targeted delete) is what correctly un-does cross-upload
    matches."""
    repo.delete_upload(upload_id)
    _rebuild_closed_trades()

    remaining = repo.list_uploads()
    if not remaining:
        return RedirectResponse(url="/upload", status_code=303)
    return RedirectResponse(url="/overview", status_code=303)


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
