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
from typing import Optional
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import repository as repo
from app.db import init_db
from app.income import event_totals, extract_cash_events, filter_income_events
from app.calc import LONG_TERM_RATE, LONG_TERM_THRESHOLD_DAYS, SHORT_TERM_RATE, enrich_trades
from app.matching import OPTION_MULTIPLIER, match_transactions
from app.parsing import extract_account_label, parse_transactions_csv
from app.quotes import random_quote
from app.transfers import filter_transfer_events
from app.summary import (
    cumulative_gain_series,
    gains_losses_by_term,
    monthly_performance,
    performance_by_month,
    performance_by_recommender,
    performance_by_ticker,
    performance_by_year,
    performance_stats,
    top_bottom_tickers,
)

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


def _inject_session_quote(request: Request) -> dict:
    """Makes the per-login trading-wisdom banner (see quotes.py) available
    in every template without threading it through each route's context
    by hand. Falls back to nothing on login.html itself (doesn't extend
    base.html, so it's simply unused there) and on any request made
    before a quote's been assigned (shouldn't happen in practice, since
    RequireLoginMiddleware already gates every other route on session
    login, which is exactly where the quote gets set)."""
    return {"quote_of_session": request.session.get("quote")}


templates = Jinja2Templates(
    directory=str(BASE_DIR / "templates"), context_processors=[_inject_session_quote]
)
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
    request.session["quote"] = random_quote()
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/")
def home(request: Request):
    uploads = repo.list_uploads()
    if not uploads:
        return templates.TemplateResponse(request, "upload.html", {"uploads": uploads, "total_upload_count": 0})
    return RedirectResponse(url="/overview")


# How many of the most recent uploads to show inline on the upload form
# itself -- past this, uploading monthly across a couple of accounts
# would turn this page into a long scroll. The full list (with account
# grouping and delete) always lives on the Overview page.
RECENT_UPLOADS_ON_FORM = 5


@app.get("/upload")
def upload_form(request: Request):
    """Always shows the upload form -- distinct from '/' so that once
    data exists, there's still a real way back here to add another
    month's statement (previously this was a dead loop)."""
    all_duplicate = request.query_params.get("all_duplicate")
    uploads = repo.list_uploads()
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            "uploads": uploads[:RECENT_UPLOADS_ON_FORM],
            "total_upload_count": len(uploads),
            "all_duplicate_skipped": int(all_duplicate) if all_duplicate else None,
        },
    )


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

    # Skip transactions already present from an earlier upload. Handles
    # broker export styles that re-include the whole year every time (a
    # "Jan 1 to today" download re-uploaded in November re-includes
    # everything from an earlier "Jan 1 to September" upload verbatim) --
    # without this, those rows would get counted twice in every total.
    existing_keys = repo.load_all_transaction_keys()
    new_transactions = [t for t in transactions if repo.transaction_key(t) not in existing_keys]
    duplicate_count = len(transactions) - len(new_transactions)

    if not new_transactions:
        # Nothing new at all -- don't create an empty upload record just
        # to immediately be useless clutter in the uploads list.
        return RedirectResponse(url=f"/upload?all_duplicate={duplicate_count}", status_code=303)

    upload_id = repo.create_upload(file.filename, account)
    for t in new_transactions:
        t.upload_id = upload_id
    repo.save_raw_transactions(upload_id, new_transactions)

    cash_events = extract_cash_events(new_transactions)
    repo.save_income_events(upload_id, cash_events)

    _rebuild_closed_trades()

    return RedirectResponse(
        url=f"/dashboard/{upload_id}?added={len(new_transactions)}&skipped={duplicate_count}",
        status_code=303,
    )


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
            "is_adjusted": t.is_adjusted,
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


@app.post("/trade_annotation")
async def save_trade_annotation(request: Request):
    """Auto-save endpoint for the Trade Log's editable Notes/Comments,
    Recommended By, and Reason columns -- called via fetch() on blur, not
    a full-page form post, so typing a note doesn't lose your current
    filters/scroll position. Kept in a separate table from closed_trades
    (see db.py) so it survives the next statement upload's full rebuild."""
    body = await request.json()
    key = body.get("trade_key", "")
    field = body.get("field", "")
    value = (body.get("value") or "").strip()
    if not key or field not in repo.ANNOTATION_FIELDS:
        return JSONResponse({"ok": False, "error": "invalid field or trade_key"}, status_code=400)
    repo.save_trade_annotation_field(key, field, value)
    return JSONResponse({"ok": True})


def _scoped_by_account(items: list[dict], account: Optional[str]) -> list[dict]:
    """Narrows a list of account-tagged dicts down to one account, if
    given. Shared between the Dashboard and Overview account filters so
    'Account: XXX111' means the exact same thing on both pages."""
    if not account:
        return items
    return [i for i in items if i.get("account") == account]


@app.get("/dashboard/{upload_id}")
def dashboard(request: Request, upload_id: str):
    uploads = repo.list_uploads()
    grouped_uploads = repo.group_uploads_by_account(uploads)
    all_trades_unfiltered = repo.load_all_closed_trades()
    # Always computed from the FULL, unfiltered history -- so the dropdown
    # keeps listing every account no matter which one is currently selected.
    accounts = sorted({t["account"] for t in all_trades_unfiltered if t.get("account")})
    selected_account = request.query_params.get("account") or None

    # "all" is a pseudo period meaning "every statement, combined" -- picked
    # from the Viewing period dropdown instead of one specific upload_id.
    # It exists because Monthly Performance/Gains-Losses-Tax/Income/
    # Transfers are normally scoped to ONE statement, which made "Account:
    # All accounts" confusingly still show just one narrow date range on
    # those tabs. This gives those tabs a real combined view without
    # having to leave the dashboard for Overview.
    viewing_all_periods = upload_id == "all"
    current_upload_id: Optional[int] = None if viewing_all_periods else int(upload_id)

    if not viewing_all_periods:
        current_upload = next((u for u in uploads if u["id"] == current_upload_id), None)
        current_upload_account = current_upload.get("account") if current_upload else None

        if selected_account and current_upload_account != selected_account:
            # A statement belongs to exactly one account. If the statement
            # you're currently viewing (via Viewing period) isn't the one
            # you just picked in the Account filter, every tab scoped to
            # "this upload + this account" (Monthly Performance, Income,
            # Transfers) would come back empty -- not because there's no
            # data, but because the two selections point at different
            # accounts. Jump to that account's most recent statement
            # instead of rendering a blank dashboard. The JS-side fix keeps
            # this from happening via the UI at all; this is the server-
            # side safety net for direct/bookmarked URLs.
            same_account_uploads = [u for u in uploads if u.get("account") == selected_account]
            if same_account_uploads:
                target_id = same_account_uploads[0]["id"]  # newest period first, already sorted
                return RedirectResponse(url=f"/dashboard/{target_id}?account={selected_account}", status_code=303)

        elif not selected_account and current_upload_account and len(accounts) > 1:
            # Vice versa: landing on one specific statement with NO account
            # filter set would show "Account: All accounts" next to a
            # Viewing period that's actually only one account's data --
            # contradictory and confusing. Make the account explicit in the
            # URL (and therefore the dropdown) instead of leaving it
            # looking broader than it really is. Preserves any other query
            # params (e.g. added=/skipped= from a fresh upload's redirect).
            params = dict(request.query_params)
            params["account"] = current_upload_account
            return RedirectResponse(url=f"/dashboard/{current_upload_id}?{urlencode(params)}", status_code=303)

    def _scoped(items: list[dict]) -> list[dict]:
        return _scoped_by_account(items, selected_account)

    all_trades = _scoped(all_trades_unfiltered)
    if viewing_all_periods:
        month_trades = all_trades
        cash_events = _scoped(repo.load_all_income_events())
    else:
        month_trades = _scoped(repo.load_closed_trades_for_upload(current_upload_id))
        cash_events = _scoped(repo.load_income_events_for_upload(current_upload_id))
    income_events = filter_income_events(cash_events)
    transfer_events = filter_transfer_events(cash_events)

    # Recomputed fresh (cheap at personal data volumes) so "needs review"
    # always reflects current book state, not a stale snapshot.
    match_result = match_transactions(repo.load_all_transactions())
    open_positions = _scoped(match_result.open_positions)
    unmatched_closes = _scoped(match_result.unmatched_closes)

    ticker_breakdown = performance_by_ticker(month_trades)
    top_tickers, bottom_tickers = top_bottom_tickers(ticker_breakdown)
    added = request.query_params.get("added")
    skipped = request.query_params.get("skipped")

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "uploads": uploads,
            "grouped_uploads": grouped_uploads,
            "current_upload_id": current_upload_id,
            "viewing_all_periods": viewing_all_periods,
            "all_trades": all_trades,
            "accounts": accounts,
            "selected_account": selected_account,
            "added_count": int(added) if added else None,
            "skipped_count": int(skipped) if skipped else 0,
            "month_trades": month_trades,
            "performance": monthly_performance(month_trades),
            "performance_stats": performance_stats(month_trades),
            "ticker_breakdown": ticker_breakdown,
            "top_tickers": top_tickers,
            "bottom_tickers": bottom_tickers,
            "upload_monthly_series": performance_by_month(month_trades),
            "term_breakdown": gains_losses_by_term(month_trades),
            "income_events": income_events,
            "income_totals": event_totals(income_events),
            "transfer_events": transfer_events,
            "transfer_totals": event_totals(transfer_events),
            "unmatched_closes": unmatched_closes,
            "open_positions": open_positions,
            "open_positions_cost_value": sum(p["cost_value"] for p in open_positions),
        },
    )


@app.get("/overview")
def overview(request: Request):
    uploads = repo.list_uploads()
    if not uploads:
        return RedirectResponse(url="/upload")

    all_trades_unfiltered = repo.load_all_closed_trades()
    # Always computed from the FULL, unfiltered history -- so the dropdown
    # keeps listing every account no matter which one is currently selected.
    accounts = sorted({t["account"] for t in all_trades_unfiltered if t.get("account")})
    selected_account = request.query_params.get("account") or None
    all_trades = _scoped_by_account(all_trades_unfiltered, selected_account)

    monthly_series = performance_by_month(all_trades)
    yearly_series = performance_by_year(all_trades)
    cumulative_points = cumulative_gain_series(all_trades)

    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "uploads": uploads,
            "accounts": accounts,
            "selected_account": selected_account,
            "performance": monthly_performance(all_trades),
            "term_breakdown": gains_losses_by_term(all_trades),
            "monthly_series": monthly_series,
            "yearly_series": yearly_series,
            "cumulative_points": cumulative_points,
            "trade_count": len(all_trades),
            "recommender_breakdown": performance_by_recommender(all_trades),
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
