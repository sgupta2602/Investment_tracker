"""End-to-end API smoke test: upload the fixture CSV through the real
FastAPI app and check the dashboard renders with sane numbers. Uses an
isolated temp DB so it never touches real data."""
import importlib
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

FIXTURE = Path(__file__).parent / "fixtures" / "sample_transactions.csv"
MULTI_MONTH_FIXTURE = Path(__file__).parent / "fixtures" / "multi_month_transactions.csv"


@pytest.fixture
def anon_client(tmp_path, monkeypatch):
    """Same setup as `client`, but WITHOUT logging in -- for testing the
    login gate itself."""
    monkeypatch.setattr("app.db.DB_PATH", tmp_path / "test.db")
    import app.main as main_module

    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c


@pytest.fixture
def client(anon_client):
    import app.main as main_module

    anon_client.post(
        "/login",
        data={"email": main_module.LOGIN_EMAIL, "password": main_module.LOGIN_PASSWORD},
    )
    return anon_client


def test_unauthenticated_visitor_redirected_to_login(anon_client):
    resp = anon_client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)
    assert resp.headers["location"] == "/login"


def test_login_with_correct_credentials_grants_access(anon_client):
    import app.main as main_module

    resp = anon_client.post(
        "/login",
        data={"email": main_module.LOGIN_EMAIL, "password": main_module.LOGIN_PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    home = anon_client.get("/")
    assert home.status_code == 200


def test_login_assigns_a_random_quote_shown_on_every_page(anon_client):
    """The trading-wisdom banner is picked once per login and injected
    into every base.html-extending page via a context processor -- not
    threaded through each route's context by hand, so this is worth
    asserting end-to-end rather than just unit-testing random_quote()."""
    import app.main as main_module
    from markupsafe import escape as html_escape

    from app.quotes import TRADING_QUOTES

    anon_client.post(
        "/login",
        data={"email": main_module.LOGIN_EMAIL, "password": main_module.LOGIN_PASSWORD},
    )
    home = anon_client.get("/")
    # Escaped the same way Jinja2 autoescaping does -- some quotes contain
    # apostrophes ("doesn't"), which render as &#39; in the HTML, so a raw
    # literal match would fail nondeterministically depending on which
    # quote random.choice() happened to pick.
    assert any(str(html_escape(q)) in home.text for q in TRADING_QUOTES)


def test_login_with_wrong_password_rejected(anon_client):
    import app.main as main_module

    resp = anon_client.post(
        "/login", data={"email": main_module.LOGIN_EMAIL, "password": "wrong-password"}
    )
    assert resp.status_code == 401


def test_account_filter_hidden_with_only_one_account(client):
    """No point cluttering the UI with a single-value dropdown."""
    with open(FIXTURE, "rb") as f:
        dashboard = client.post(
            "/upload", files={"file": ("sample_transactions.csv", f, "text/csv")}
        )
    assert 'id="account-select"' not in dashboard.text


def test_account_filter_appears_and_lists_distinct_accounts_when_multiple(client):
    """Two statements from two different (masked) account numbers must
    both show up as filter options in the global Account dropdown. The
    account you JUST uploaded (and are now viewing) should come back
    pre-selected -- landing on a specific statement with 'Account: All
    accounts' still showing would be a contradiction (see the vice-versa
    auto-sync test below)."""
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX111_Transactions.csv", f, "text/csv")})
    with open(FIXTURE, "rb") as f:
        dashboard = client.post("/upload", files={"file": ("Joint_Tenant_XX222_Transactions.csv", f, "text/csv")})

    assert 'id="account-select"' in dashboard.text
    assert '<option value="XX111" >XX111</option>' in dashboard.text
    assert '<option value="XX222" selected>XX222</option>' in dashboard.text


def test_selecting_an_account_scopes_every_tab_to_just_that_account(client):
    """The whole point of the global filter: picking Account=XX111 on the
    dashboard URL must narrow Trade Log, Needs Review, and Income/Transfers
    down to just that account's rows -- not merely hide some table rows
    client-side while leaving everything else showing the combined book."""
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX111_Transactions.csv", f, "text/csv")})
    with open(FIXTURE, "rb") as f:
        upload2 = client.post("/upload", files={"file": ("Joint_Tenant_XX222_Transactions.csv", f, "text/csv")})

    # TestClient follows redirects, so pull the current upload's id off the rendered <select>.
    upload_id = re.search(r'<option value="(\d+)"[^>]*selected', upload2.text).group(1)

    combined = client.get(f"/dashboard/{upload_id}")
    scoped = client.get(f"/dashboard/{upload_id}?account=XX111")

    assert '<td class="px-2 py-1 text-slate-300">XX222</td>' in combined.text
    assert '<td class="px-2 py-1 text-slate-300">XX222</td>' not in scoped.text
    assert '<td class="px-2 py-1 text-slate-300">XX111</td>' in scoped.text
    # The Account dropdown itself must still offer XX222 even while scoped
    # to XX111 -- switching back and forth has to stay possible.
    assert '<option value="XX222"' in scoped.text
    # And the currently active filter must show as selected in that dropdown.
    assert '<option value="XX111" selected>XX111</option>' in scoped.text


def test_switching_to_a_different_account_redirects_off_a_mismatched_period(client):
    """Regression test: a statement belongs to exactly one account. If
    you're viewing XX222's statement and pick Account=XX111 in the
    filter, staying on XX222's upload_id would make Monthly Performance,
    Income, and Transfers all render empty (they're scoped to THIS
    upload's transactions, further filtered by account -- an upload from
    a different account has zero rows left after that filter). The
    dashboard must instead redirect to XX111's own most recent statement."""
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX111_Transactions.csv", f, "text/csv")})
    with open(FIXTURE, "rb") as f:
        upload2 = client.post("/upload", files={"file": ("Joint_Tenant_XX222_Transactions.csv", f, "text/csv")})
    upload2_id = re.search(r'<option value="(\d+)"[^>]*selected', upload2.text).group(1)

    resp = client.get(f"/dashboard/{upload2_id}?account=XX111", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "account=XX111" in location
    assert f"/dashboard/{upload2_id}?" not in location  # must NOT stay on XX222's statement

    followed = client.get(location)
    # The fixture's own trades and dividends must show up -- proving
    # Monthly Performance and Income are scoped to XX111's OWN statement,
    # not still pointed at XX222's (which would render both empty).
    assert "No closed trades in this upload." not in followed.text
    assert "No income events in this upload." not in followed.text


def test_viewing_period_dropdown_disables_other_accounts_optgroup(client):
    """UX fix for the same bug: while Account=XX111 is active, XX222's
    periods should show as disabled in the Viewing period dropdown --
    they used to be pickable and would silently produce an empty
    dashboard once picked."""
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX111_Transactions.csv", f, "text/csv")})
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX222_Transactions.csv", f, "text/csv")})

    scoped = client.get("/dashboard/1?account=XX111", follow_redirects=True)
    assert 'data-account="XX111"' in scoped.text
    assert 'data-account="XX222"' in scoped.text
    assert "syncUploadOptionsToAccount" in scoped.text
    assert "group.disabled = !account || group.dataset.account !== account" in scoped.text


def test_viewing_period_dropdown_disables_every_account_when_all_accounts_selected(client):
    """The other half of the same UX fix: with 'Account: All accounts'
    active, NEITHER account's individual periods should be pickable in
    Viewing period -- only after choosing a specific account do that
    account's own periods become selectable. 'All periods (combined)'
    stays selectable regardless (it isn't tied to one account)."""
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX111_Transactions.csv", f, "text/csv")})
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX222_Transactions.csv", f, "text/csv")})

    all_accounts = client.get("/dashboard/all")
    assert 'value="all" selected' in all_accounts.text
    assert 'data-account="XX111"' in all_accounts.text
    assert 'data-account="XX222"' in all_accounts.text
    # JS disables every optgroup up front (empty account value); no
    # server-rendered "disabled" needed since this is applied client-side.
    assert "group.disabled = !account" in all_accounts.text
    # And switching TO "All accounts" while viewing one specific statement
    # must fall back to "all" client-side, instead of bouncing right back
    # to the account just left (the server would otherwise re-infer it).
    assert 'uploadId = "all"' in all_accounts.text


def test_landing_on_one_statement_auto_selects_its_own_account(client):
    """Vice versa of the account -> period sync: landing on ONE specific
    statement with no ?account= in the URL must auto-redirect to make
    that statement's own account explicit, instead of showing 'Account:
    All accounts' next to what is actually just one account's data."""
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX111_Transactions.csv", f, "text/csv")})
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX222_Transactions.csv", f, "text/csv")})

    resp = client.get("/dashboard/1", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/1?account=XX111"

    followed = client.get(resp.headers["location"])
    assert '<option value="XX111" selected>XX111</option>' in followed.text


def test_auto_account_redirect_preserves_other_query_params(client):
    """The fresh-upload redirect (POST /upload -> /dashboard/{id}?added=N)
    must survive having ?account= appended -- losing 'added'/'skipped'
    would silently drop the 'Added N new transactions' banner."""
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX111_Transactions.csv", f, "text/csv")})
    with open(FIXTURE, "rb") as f:
        upload2 = client.post("/upload", files={"file": ("Joint_Tenant_XX222_Transactions.csv", f, "text/csv")})

    redirect_locations = [str(r.headers.get("location", "")) for r in upload2.history]
    assert any("account=XX222" in loc and "added=" in loc for loc in redirect_locations)
    assert "Added" in upload2.text


def test_dashboard_all_periods_combines_every_statement(client):
    """Picking 'All periods (combined)' from the Viewing period dropdown
    must make Monthly Performance/Income/Transfers behave like Overview
    -- full history, not one statement -- instead of forcing you to leave
    the dashboard to see combined totals."""
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX111_A.csv", f, "text/csv")})
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX222_B.csv", f, "text/csv")})

    single = client.get("/dashboard/1")
    combined = client.get("/dashboard/all")

    assert 'value="all" selected' in combined.text
    assert "No closed trades" not in combined.text
    assert "No income events" not in combined.text

    # Combined trade count on Monthly Performance must be double a single
    # statement's -- proving it's actually pulling every upload, not just
    # relabeling the same single-statement numbers.
    single_trades = int(re.search(r'<dt class="text-xs uppercase tracking-wide text-slate-500">Trades</dt>\s*<dd[^>]*>(\d+)', single.text).group(1))
    combined_trades = int(re.search(r'<dt class="text-xs uppercase tracking-wide text-slate-500">Trades</dt>\s*<dd[^>]*>(\d+)', combined.text).group(1))
    assert combined_trades == single_trades * 2

    # No single statement to delete while viewing the combined view.
    assert "Delete this statement" not in combined.text


def test_dashboard_all_periods_respects_the_account_filter(client):
    """'All periods' scoped to one account must show just that account's
    combined history, not everyone's -- and switching accounts while
    already on 'All periods' should stay on 'All periods' rather than
    getting bounced to one specific statement."""
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX111_A.csv", f, "text/csv")})
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX222_B.csv", f, "text/csv")})

    combined = client.get("/dashboard/all")
    scoped = client.get("/dashboard/all?account=XX111", follow_redirects=False)

    assert scoped.status_code == 200  # no mismatch redirect -- "all" is valid for any account
    combined_trades = int(re.search(r'<dt class="text-xs uppercase tracking-wide text-slate-500">Trades</dt>\s*<dd[^>]*>(\d+)', combined.text).group(1))
    scoped_trades = int(re.search(r'<dt class="text-xs uppercase tracking-wide text-slate-500">Trades</dt>\s*<dd[^>]*>(\d+)', scoped.text).group(1))
    assert scoped_trades == combined_trades // 2
    assert 'value="all" selected' in scoped.text


def test_months_filter_appears_only_in_all_periods_view(client):
    """The 'Filter by month' checkboxes only make sense once you're
    looking at combined history -- one single statement is already just
    one narrow window, nothing to pick from."""
    with open(MULTI_MONTH_FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Statement.csv", f, "text/csv")})

    single = client.get("/dashboard/1")
    combined = client.get("/dashboard/all")

    assert "months-filter-details" not in single.text
    assert "months-filter-details" in combined.text
    assert "Jan 2026" in combined.text
    assert "Mar 2026" in combined.text
    assert "Jun 2026" in combined.text


def test_selecting_non_contiguous_months_filters_every_scoped_tab(client):
    """The whole point of this filter: pick Jan + Jun, skip Mar entirely,
    and every statement-scoped tab (Trade Log, Monthly Performance,
    Income) reflects only those two months -- not a single from/to range,
    which couldn't express this combination."""
    with open(MULTI_MONTH_FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Statement.csv", f, "text/csv")})

    resp = client.get("/dashboard/all?months=2026-01,2026-06")

    # Trade Log: Jan (JANC) and Jun (JUNC) show up, March (MARC) doesn't.
    assert "JANC" in resp.text
    assert "JUNC" in resp.text
    assert "MARC" not in resp.text

    # Income: Jan ($15.50) and Jun ($20.00) dividends show, March's
    # ($99.00) is filtered out.
    assert "$15.50" in resp.text
    assert "$20.00" in resp.text
    assert "$99.00" not in resp.text

    # Both checkboxes come back checked, March's does not.
    def _is_checked(month_key: str) -> bool:
        snippet = re.search(r'value="' + month_key + r'"(.*?)>', resp.text, re.DOTALL)
        return bool(snippet and "checked" in snippet.group(1))

    assert _is_checked("2026-01")
    assert _is_checked("2026-06")
    assert not _is_checked("2026-03")


def test_clearing_months_filter_restores_full_combined_history(client):
    """Dropping the ?months= param (the 'Clear' button) must bring back
    all three months, not leave you stuck on the last selection."""
    with open(MULTI_MONTH_FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Statement.csv", f, "text/csv")})

    filtered = client.get("/dashboard/all?months=2026-01")
    cleared = client.get("/dashboard/all")

    assert "MARC" not in filtered.text
    assert "JANC" in cleared.text and "MARC" in cleared.text and "JUNC" in cleared.text


def test_invalid_month_key_in_query_param_is_silently_ignored(client):
    """A stale/bogus ?months= value (e.g. from an old bookmark after data
    changed) shouldn't blow up or silently match everything -- it's just
    dropped, same as picking zero real months."""
    with open(MULTI_MONTH_FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Statement.csv", f, "text/csv")})

    resp = client.get("/dashboard/all?months=1999-01,garbage")

    assert resp.status_code == 200
    assert "JANC" in resp.text and "MARC" in resp.text and "JUNC" in resp.text


def test_logout_revokes_access(client):
    client.post("/logout")
    resp = client.get("/", follow_redirects=False)
    assert resp.headers["location"] == "/login"


def test_upload_then_dashboard_shows_closed_trades(client):
    with open(FIXTURE, "rb") as f:
        resp = client.post(
            "/upload",
            files={"file": ("sample_transactions.csv", f, "text/csv")},
        )
    assert resp.status_code == 200  # TestClient follows the 303 redirect
    assert "ABCD" in resp.text
    # Plain share Buy/Sell trades are matched too, right alongside options.
    assert "OLDCO" in resp.text
    assert "Trade Log" in resp.text


def test_income_cards_are_clickable_filters_over_the_events_table(client):
    """Same reasoning as the trade log filter test: this is pure
    client-side JS wired up via data attributes, so assert directly on
    the markup those attributes since JS logic bugs wouldn't otherwise
    surface as a server-side error."""
    with open(FIXTURE, "rb") as f:
        resp = client.post(
            "/upload",
            files={"file": ("sample_transactions.csv", f, "text/csv")},
        )
    assert 'data-filter-label="Cash Dividend"' in resp.text
    assert 'data-label="Cash Dividend"' in resp.text
    assert "filterIncomeByLabel" in resp.text
    assert 'id="income-filter-status"' in resp.text


def test_withdrawal_and_journal_transfer_are_on_transfers_tab_not_income(client):
    """The whole point of splitting these into their own tab: Withdrawal
    and Journal Transfer must render on the Transfers panel, and must
    NOT appear anywhere on the Income panel."""
    csv_content = (
        '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
        '"01/05/2026","Cash Dividend","","DIVIDEND","","","","$12.50"\n'
        '"01/10/2026","MoneyLink Transfer","","TRANSFER TO BANK","","","","-$2500.00"\n'
        '"01/15/2026","Journal","","JOURNAL TO ...556","","","","-$1000.00"\n'
    )
    resp = client.post("/upload", files={"file": ("transfers.csv", csv_content, "text/csv")})

    # Extract just the Income panel's markup so a match doesn't leak in
    # from the Transfers panel sitting elsewhere on the same page.
    income_panel = re.search(r'id="tab-income".*?id="tab-transfers"', resp.text, re.S).group(0)
    assert "Cash Dividend" in income_panel
    assert "Withdrawal" not in income_panel
    assert "Journal Transfer" not in income_panel

    transfers_panel = re.search(r'id="tab-transfers".*?id="tab-review"', resp.text, re.S).group(0)
    assert "Withdrawal" in transfers_panel
    assert "Journal Transfer (to Another Account)" in transfers_panel
    assert "Cash Dividend" not in transfers_panel


def test_transfer_cards_are_clickable_filters_over_the_events_table(client):
    csv_content = (
        '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
        '"01/10/2026","MoneyLink Transfer","","TRANSFER TO BANK","","","","-$2500.00"\n'
    )
    resp = client.post("/upload", files={"file": ("transfers.csv", csv_content, "text/csv")})
    assert 'data-filter-label="Withdrawal"' in resp.text
    assert 'data-label="Withdrawal"' in resp.text
    assert "filterTransferByLabel" in resp.text
    assert 'id="transfer-filter-status"' in resp.text


def test_trade_log_rows_carry_search_and_date_filter_attributes(client):
    """The ticker search + date range filter are pure client-side JS over
    these data attributes -- if they're missing/wrong, filtering silently
    does nothing (or filters wrong rows) with no server-side error to catch
    it, so this is worth asserting on directly."""
    with open(FIXTURE, "rb") as f:
        resp = client.post(
            "/upload",
            files={"file": ("sample_transactions.csv", f, "text/csv")},
        )
    assert 'data-ticker="abcd"' in resp.text.lower()
    assert 'id="ticker-search"' in resp.text
    assert 'id="date-from"' in resp.text
    assert 'id="date-to"' in resp.text


def test_trade_log_shows_both_options_and_shares_as_distinct_rows(client):
    """The exact ask: plain stock Buy/Sell trades (OLDCO) should show up
    in the same Trade Log as options (ABCD), each clearly tagged with its
    own Equity Type -- never merged or mistaken for one another."""
    with open(FIXTURE, "rb") as f:
        resp = client.post("/upload", files={"file": ("sample_transactions.csv", f, "text/csv")})

    assert "ABCD" in resp.text
    assert "OLDCO" in resp.text
    assert 'data-equity-type="options"' in resp.text
    assert 'data-equity-type="shares"' in resp.text
    assert 'id="equity-type-filter"' in resp.text


def test_share_trade_never_shows_up_multiplied_like_an_option_contract(client):
    """Regression guard for the 100x options multiplier leaking into share
    quantities -- OLDCO bought/sold 100 real shares should show '100' in
    Qty, not 10000 (100 shares * the options multiplier)."""
    with open(FIXTURE, "rb") as f:
        resp = client.post("/upload", files={"file": ("sample_transactions.csv", f, "text/csv")})

    oldco_row = re.search(r'data-ticker="oldco"[^>]*>(.*?)</tr>', resp.text, re.DOTALL)
    assert oldco_row is not None
    assert ">100<" in oldco_row.group(1)
    assert "10000" not in oldco_row.group(1)


def test_trade_log_has_a_live_total_gain_loss_footer_row(client):
    """The Total Gain/Loss footer is computed client-side (JS sums
    data-gain-loss across whatever rows are currently visible), so this
    asserts the plumbing it depends on: the footer cell itself, and a
    raw numeric data-gain-loss attribute per row for the JS to add up."""
    with open(FIXTURE, "rb") as f:
        resp = client.post("/upload", files={"file": ("sample_transactions.csv", f, "text/csv")})

    assert 'id="trade-log-total-gain"' in resp.text
    # ABCD's real gain, WXYZ's expiration loss, and OLDCO's share gain --
    # raw floats (JS parseFloat()s and sums them, then formats to 2dp),
    # so this just confirms each row carries its true numeric value.
    assert 'data-gain-loss="397.35999999999996"' in resp.text
    assert 'data-gain-loss="-200.66000000000003"' in resp.text
    assert 'data-gain-loss="499.0"' in resp.text
    assert "totalGain" in resp.text  # the summing logic itself is present


def test_trade_log_has_a_live_total_est_tax_footer_row(client):
    """Same reasoning and same live-filtering behavior as the Total
    Gain/Loss footer, just for Est. Tax -- lets you see the tax bill for
    just a filtered slice (a ticker, a date range, Options vs Shares)."""
    with open(FIXTURE, "rb") as f:
        resp = client.post("/upload", files={"file": ("sample_transactions.csv", f, "text/csv")})

    assert 'id="trade-log-total-tax"' in resp.text
    assert "Total Est. Tax" in resp.text
    # OLDCO (long-term, 20%), ABCD (short-term, 37%), WXYZ (a loss -> $0 tax).
    assert 'data-est-tax="99.80000000000001"' in resp.text
    assert 'data-est-tax="147.02319999999997"' in resp.text
    assert 'data-est-tax="0.0"' in resp.text
    assert "totalTax" in resp.text


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


def test_upload_form_caps_recent_uploads_and_links_to_overview_for_the_rest(client):
    """Uploading a statement every month across a couple of accounts
    would turn this page into a long scroll if every upload rendered
    inline -- only the most recent RECENT_UPLOADS_ON_FORM should show,
    with a link out to the Overview page for the complete list."""
    import app.main as main_module

    limit = main_module.RECENT_UPLOADS_ON_FORM
    total = limit + 2
    for i in range(total):
        # Distinct masked account per upload so none of these are
        # treated as duplicates of each other and skipped.
        with open(FIXTURE, "rb") as f:
            client.post("/upload", files={"file": (f"Statement_XX{i}00_Transactions.csv", f, "text/csv")})

    resp = client.get("/upload")
    assert resp.text.count("/dashboard/") == limit
    assert f"most recent {limit} of {total}" in resp.text
    assert f"View all {total} statements" in resp.text


def test_overview_shows_performance_charts_after_upload(client):
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("sample_transactions.csv", f, "text/csv")})
    resp = client.get("/overview")
    assert resp.status_code == 200
    assert "cumulativeChart" in resp.text
    assert "termChart" in resp.text
    assert "monthlyChart" in resp.text
    assert "yearlyChart" in resp.text
    assert "Year-over-Year Gain" in resp.text


def test_overview_account_filter_hidden_with_only_one_account(client):
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("sample_transactions.csv", f, "text/csv")})
    resp = client.get("/overview")
    assert 'id="account-select"' not in resp.text


def test_overview_account_filter_scopes_every_number_to_one_account(client):
    """The whole point: picking Account=XX111 on Overview must re-derive
    the trade count (and everything downstream of it -- Realized
    Investment, Total Gain, the charts) from just that account's
    trades, not merely relabel the combined totals. Default with no
    filter must stay 'all accounts' combined."""
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX111_Transactions.csv", f, "text/csv")})
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX222_Transactions.csv", f, "text/csv")})

    combined = client.get("/overview")
    assert ", all accounts)" in combined.text
    combined_count = int(re.search(r"\((\d+) closed trades", combined.text).group(1))

    scoped = client.get("/overview?account=XX111")
    assert "for XX111)" in scoped.text
    scoped_count = int(re.search(r"\((\d+) closed trades", scoped.text).group(1))

    # Same fixture uploaded under two distinct accounts -> exactly half
    # the combined trades belong to just one of them.
    assert scoped_count == combined_count // 2
    assert scoped_count > 0
    assert '<option value="XX111" selected>XX111</option>' in scoped.text


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


def test_bulk_delete_removes_multiple_selected_statements_in_one_go(client):
    """The Overview page's checkbox-driven bulk delete: check several
    statements, hit one 'Delete selected' button, and all of them go
    away together instead of one confirm dialog per statement."""
    with open(MULTI_MONTH_FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX111_A.csv", f, "text/csv")})
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX222_B.csv", f, "text/csv")})
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("Joint_Tenant_XX333_C.csv", f, "text/csv")})

    overview_before = client.get("/overview")
    assert "Joint_Tenant_XX111_A.csv" in overview_before.text
    assert "Joint_Tenant_XX222_B.csv" in overview_before.text
    assert "Joint_Tenant_XX333_C.csv" in overview_before.text

    resp = client.post("/delete_uploads", data={"upload_ids": ["1", "3"]}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/overview"

    overview_after = client.get("/overview")
    assert "Joint_Tenant_XX111_A.csv" not in overview_after.text  # deleted
    assert "Joint_Tenant_XX333_C.csv" not in overview_after.text  # deleted
    assert "Joint_Tenant_XX222_B.csv" in overview_after.text  # left alone


def test_bulk_delete_all_statements_redirects_to_upload_form(client):
    """Same 'nothing left -> go upload something' rule as the single
    delete, just reached by selecting every checkbox."""
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("A.csv", f, "text/csv")})
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("B.csv", f, "text/csv")})

    resp = client.post("/delete_uploads", data={"upload_ids": ["1", "2"]}, follow_redirects=False)
    assert resp.headers["location"] == "/upload"


def test_bulk_delete_with_no_ids_selected_is_a_harmless_no_op(client):
    """Submitting the bulk form with nothing checked (shouldn't normally
    be reachable -- the button is disabled client-side -- but a direct
    POST shouldn't wipe anything or error either."""
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("A.csv", f, "text/csv")})

    resp = client.post("/delete_uploads", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/overview"

    overview = client.get("/overview")
    assert "A.csv" in overview.text  # still there


def test_overview_bulk_delete_ui_present(client):
    """Sanity check the checkbox column, select-all, and bulk button
    actually render."""
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("A.csv", f, "text/csv")})

    overview = client.get("/overview")
    assert 'id="select-all-uploads"' in overview.text
    assert 'name="upload_ids" value="1"' in overview.text
    assert 'id="bulk-delete-btn"' in overview.text
    assert 'action="/delete_uploads"' in overview.text


def _extract_trade_key(html: str, ticker: str) -> str:
    """Pulls the data-trade-key attribute for the first row matching a
    given ticker out of the rendered Trade Log HTML."""
    match = re.search(
        rf'data-ticker="{ticker.lower()}"[^>]*>.*?data-trade-key="([^"]+)"',
        html,
        re.DOTALL,
    )
    assert match, f"couldn't find a trade-key for ticker {ticker!r} in the rendered page"
    return match.group(1)


def test_trade_annotation_saves_and_shows_up_on_reload(client):
    with open(FIXTURE, "rb") as f:
        dashboard = client.post(
            "/upload", files={"file": ("sample_transactions.csv", f, "text/csv")}
        )
    key = _extract_trade_key(dashboard.text, "ABCD")

    resp = client.post(
        "/trade_annotation",
        json={"trade_key": key, "field": "recommended_by", "value": "Cousin Raj"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    reloaded = client.get("/dashboard/1")
    assert "Cousin Raj" in reloaded.text


def test_trade_annotation_rejects_unknown_field(client):
    resp = client.post(
        "/trade_annotation",
        json={"trade_key": "whatever", "field": "not_a_real_field", "value": "x"},
    )
    assert resp.status_code == 400


def test_trade_annotation_survives_a_second_statement_upload(client):
    """The whole point of the separate trade_annotations table: a new
    statement upload triggers _rebuild_closed_trades(), which fully
    DELETE+reinserts closed_trades. A note typed in before that upload
    must still be there after it, not silently wiped."""
    with open(FIXTURE, "rb") as f:
        dashboard = client.post(
            "/upload", files={"file": ("sample_transactions.csv", f, "text/csv")}
        )
    key = _extract_trade_key(dashboard.text, "ABCD")
    client.post(
        "/trade_annotation",
        json={"trade_key": key, "field": "notes", "value": "Doubled down after the split"},
    )

    # A second, unrelated statement upload -- triggers a full closed_trades rebuild.
    other_csv = (
        '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
        '"03/01/2026","Buy to Open","QRS 05/01/2026 20.00 C","CALL QRS","1","$1.00","$0.10","-$100.10"\n'
        '"03/15/2026","Sell to Close","QRS 05/01/2026 20.00 C","CALL QRS","1","$2.00","$0.10","$199.90"\n'
    )
    client.post("/upload", files={"file": ("other.csv", other_csv, "text/csv")})

    reloaded = client.get("/dashboard/1")
    assert "Doubled down after the split" in reloaded.text


def test_recommended_by_shows_up_on_overview_breakdown(client):
    with open(FIXTURE, "rb") as f:
        dashboard = client.post(
            "/upload", files={"file": ("sample_transactions.csv", f, "text/csv")}
        )
    key = _extract_trade_key(dashboard.text, "ABCD")
    client.post(
        "/trade_annotation",
        json={"trade_key": key, "field": "recommended_by", "value": "Cousin Raj"},
    )

    overview = client.get("/overview")
    assert overview.status_code == 200
    assert "Cousin Raj" in overview.text
    assert "ABCD" in overview.text  # ticker shows in that recommender's row


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


def test_reuploading_a_superset_statement_skips_already_known_transactions(client):
    """Simulates a broker export style that always starts from Jan 1 --
    re-uploading a 'Jan-Nov' file that re-includes an already-uploaded
    'Jan-Sep' file's rows verbatim, plus new ones, must only add the new
    ones and must NOT double-count the overlapping trade."""
    feb_csv = (
        '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
        '"02/10/2026","Buy to Open","XYZ 10/16/2026 90.00 C","CALL XYZ","1","$3.35","$0.66","-$335.66"\n'
        '"02/15/2026","Sell to Close","XYZ 10/16/2026 90.00 C","CALL XYZ","1","$8.20","$0.66","$819.34"\n'
    )
    feb_plus_june_csv = (
        '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
        '"02/10/2026","Buy to Open","XYZ 10/16/2026 90.00 C","CALL XYZ","1","$3.35","$0.66","-$335.66"\n'
        '"02/15/2026","Sell to Close","XYZ 10/16/2026 90.00 C","CALL XYZ","1","$8.20","$0.66","$819.34"\n'
        '"06/01/2026","Buy to Open","QRS 08/01/2026 20.00 C","CALL QRS","1","$1.00","$0.10","-$100.10"\n'
        '"06/15/2026","Sell to Close","QRS 08/01/2026 20.00 C","CALL QRS","1","$2.00","$0.10","$199.90"\n'
    )
    client.post("/upload", files={"file": ("feb.csv", feb_csv, "text/csv")})
    dashboard = client.post(
        "/upload", files={"file": ("feb_plus_june.csv", feb_plus_june_csv, "text/csv")}
    )

    # Only the 2 new June rows should have been added -- not the 2 Feb rows again.
    assert "Added 2 new transaction" in dashboard.text
    assert "Skipped 2" in dashboard.text

    overview = client.get("/overview")
    assert "2 closed trades" in overview.text  # XYZ once + QRS once, not XYZ twice


def test_reuploading_a_fully_duplicate_statement_creates_no_new_upload(client):
    """Uploading the exact same file twice must not create a second,
    empty-of-anything-new upload record just to clutter the list."""
    with open(FIXTURE, "rb") as f:
        client.post("/upload", files={"file": ("sample_transactions.csv", f, "text/csv")})
    with open(FIXTURE, "rb") as f:
        resp = client.post(
            "/upload", files={"file": ("sample_transactions.csv", f, "text/csv")}
        )

    assert resp.status_code == 200  # TestClient followed the redirect back to /upload
    assert "already in the system" in resp.text

    uploads = client.get("/upload")
    # Still only the one original upload -- no phantom second entry.
    # (The filename legitimately appears twice per listed upload: once
    # in the link text, once in the delete-confirm dialog string -- so
    # count the dashboard links instead of the raw filename string.)
    assert uploads.text.count('href="/dashboard/') == 1


def test_still_open_positions_show_cost_value_and_grand_total(client):
    """Cost Value = units * open price -- how much money is actually at
    stake in a position with no closing trade yet. Plus a grand total
    across all open positions, so the number that matters most (total
    money at risk right now) doesn't require manual addition."""
    csv_content = (
        '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
        '"01/05/2026","Buy to Open","AAA 06/19/2026 50.00 C","CALL AAA","2","$3.00","$0.00","-$600.00"\n'
        '"01/06/2026","Buy to Open","BBB 06/19/2026 20.00 P","PUT BBB","1","$1.50","$0.00","-$150.00"\n'
    )
    dashboard = client.post("/upload", files={"file": ("open_positions.csv", csv_content, "text/csv")})

    # AAA: 200 units (2 contracts * 100) * $3.00 = $600.00
    assert "$600.00" in dashboard.text
    # BBB: 100 units * $1.50 = $150.00
    assert "$150.00" in dashboard.text
    # Grand total across both: $750.00
    assert "$750.00" in dashboard.text
    assert "Total money at stake" in dashboard.text


def test_still_open_positions_carry_search_and_date_filter_attributes(client):
    """Same reasoning as the Trade Log filter test -- these are pure
    client-side JS filters over data attributes, so wrong/missing
    attributes fail silently in the browser with no server error."""
    csv_content = (
        '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
        '"01/05/2026","Buy to Open","AAA 06/19/2026 50.00 C","CALL AAA","2","$3.00","$0.00","-$600.00"\n'
    )
    dashboard = client.post("/upload", files={"file": ("open_positions.csv", csv_content, "text/csv")})
    assert 'data-ticker="aaa"' in dashboard.text.lower()
    assert 'data-open-date="2026-01-05"' in dashboard.text
    assert 'id="open-ticker-search"' in dashboard.text
    assert 'id="open-date-from"' in dashboard.text
    assert 'id="open-date-to"' in dashboard.text
    assert "filterOpenPositions" in dashboard.text


def test_open_stock_position_shows_equity_type_distinct_from_open_option(client):
    """An open share position (no closing Sell yet) and an open option
    position should sit side by side on Needs Review, each tagged with
    its own Equity Type -- exactly the 'no overlap or confusion' ask."""
    csv_content = (
        '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
        '"01/05/2026","Buy to Open","AAA 06/19/2026 50.00 C","CALL AAA","2","$3.00","$0.00","-$600.00"\n'
        '"01/06/2026","Buy","NEWCO","NEW COMPANY INC","50","$4.00","$0.00","-$200.00"\n'
    )
    dashboard = client.post("/upload", files={"file": ("mixed_open.csv", csv_content, "text/csv")})
    assert "AAA" in dashboard.text
    assert "NEWCO" in dashboard.text
    assert 'data-equity-type="options"' in dashboard.text
    assert 'data-equity-type="shares"' in dashboard.text
    assert 'id="open-equity-type-filter"' in dashboard.text


def test_open_positions_total_carries_cost_value_for_live_filtering(client):
    """Regression guard: the 'Total money at stake' footer used to be a
    fixed server-rendered number that ignored the Type/ticker/date
    filters entirely. It's now live JS, summing data-cost-value across
    whichever rows are currently visible -- this asserts the plumbing
    it depends on (the raw per-row values and the footer cell/JS)."""
    csv_content = (
        '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
        '"01/05/2026","Buy to Open","AAA 06/19/2026 50.00 C","CALL AAA","2","$3.00","$0.00","-$600.00"\n'
        '"01/06/2026","Buy","NEWCO","NEW COMPANY INC","50","$4.00","$0.00","-$200.00"\n'
    )
    dashboard = client.post("/upload", files={"file": ("mixed_open.csv", csv_content, "text/csv")})
    assert 'id="open-positions-total-cost-value"' in dashboard.text
    assert 'data-cost-value="600.0"' in dashboard.text  # AAA: 200 units * $3.00
    assert 'data-cost-value="200.0"' in dashboard.text  # NEWCO: 50 units * $4.00
    assert "totalCostValue" in dashboard.text


def test_needs_review_sections_show_counts_and_sequential_row_numbers(client):
    """Both Needs Review tables (and the main Trade Log) should show a
    running '#' index down the left and the total count of rows right
    in each section heading, so 'how many do I have' never requires
    manually counting rows."""
    csv_content = (
        '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
        '"01/05/2026","Buy to Open","AAA 06/19/2026 50.00 C","CALL AAA","2","$3.00","$0.00","-$600.00"\n'
        '"01/06/2026","Buy to Open","BBB 06/19/2026 20.00 P","PUT BBB","1","$1.50","$0.00","-$150.00"\n'
        '"02/01/2026","Sell to Close","CCC 06/19/2026 10.00 C","CALL CCC","1","$2.00","$0.00","$200.00"\n'
    )
    dashboard = client.post("/upload", files={"file": ("needs_review.csv", csv_content, "text/csv")})
    assert "Still-open positions (no closing trade yet)" in dashboard.text
    assert "<span class=\"text-slate-500 font-normal\">(2)</span>" in dashboard.text
    assert "Closed positions missing an opening trade" in dashboard.text
    assert "<span class=\"text-slate-500 font-normal\">(1)</span>" in dashboard.text


def test_trade_log_shows_sequential_row_numbers(client):
    with open(FIXTURE, "rb") as f:
        resp = client.post("/upload", files={"file": ("sample_transactions.csv", f, "text/csv")})
    assert '<th scope="col" class="px-2 py-2 text-right">#</th>' in resp.text
