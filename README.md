# Investment Tracker

Turns a raw broker transaction CSV export into the same trade-log,
monthly-performance, and gain/loss-tax analytics that used to be a manual
Excel exercise every month.

## What it does

1. Upload a broker transaction CSV (Schwab-style: Date, Action, Symbol,
   Description, Quantity, Price, Fees & Comm, Amount).
2. The app reconstructs closed trade-pairs using FIFO lot matching:
   - `Buy to Open` -> `Sell to Close` / `Expired` for options
   - `Buy` -> `Sell` for shares
3. Every closed trade gets the same math the old spreadsheet used:
   realized value, cost basis, holding period, gain/loss, % gain/loss,
   gain-per-month, Short/Long-term classification (> 365 days = Long),
   cumulative running totals, and estimated tax.
4. A "Needs Review" tab surfaces anything that couldn't be fully matched
   (e.g. a position closed here but opened in a statement you haven't
   uploaded yet) plus still-open positions -- nothing is silently dropped.
5. A bonus "Income" tab shows dividends, ADR fees, foreign tax, and
   transfers for context (these don't feed into the tax numbers).

## Key decisions (confirmed with the user)

- Short-term capital gains tax: flat **37%**. Long-term: **20%**. Only
  applied to gains, never losses -- matches the original sheet.
- Lot matching: **FIFO**.
- Fees & commissions are **folded into cost basis / proceeds** (a
  deliberate, more tax-accurate departure from the original sheet, which
  ignored them).
- Single account for now. Multi-account support would mean threading an
  account selector through the queries -- straightforward to add later.
- Cumulative columns run across **all uploads ever made**, not just the
  current one -- mirrors the spreadsheet's running-total columns, but
  now it never resets when you start a new tab/month.

## Running it

```bash
uv venv
source .venv/bin/activate
uv pip install --index-url https://pypi.ci.artifacts.walmart.com/artifactory/api/pypi/external-pypi/simple \
  --allow-insecure-host pypi.ci.artifacts.walmart.com \
  fastapi uvicorn jinja2 python-multipart

uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000 and upload a CSV.

## Running tests

```bash
source .venv/bin/activate
uv pip install --index-url https://pypi.ci.artifacts.walmart.com/artifactory/api/pypi/external-pypi/simple \
  --allow-insecure-host pypi.ci.artifacts.walmart.com pytest httpx
python -m pytest
```

## Project layout

```
app/
  parsing.py     -- CSV -> Transaction records (dates, option symbols, money)
  matching.py    -- FIFO lot matching -> ClosedTrade records
  calc.py        -- per-trade formulas (gain/loss, holding period, tax)
  summary.py     -- monthly performance + gains/losses/tax roll-ups
  income.py      -- dividends/fees/transfers extraction
  repository.py  -- SQLite persistence
  db.py          -- schema + connection
  main.py        -- FastAPI routes
  templates/     -- Jinja2 + Tailwind (CDN) + vanilla JS tabs
tests/           -- pytest suite (synthetic fixture, no real account data)
```

## Data privacy note

Real broker CSVs and the SQLite database are git-ignored (`*.csv`, `*.db`,
`uploads/`). Tests use a small synthetic fixture, not real account data.
