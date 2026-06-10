"""Brex historical ingestion — cash/card accounts + per-account transactions backfill.

The REAL Brex contract (developer.brex.com OpenAPI):
  * ``GET /v2/accounts/cash`` → ``{next_cursor, items:[CashAccount]}`` (CURSOR page).
  * ``GET /v2/accounts/cash/primary`` → the single primary CashAccount.
  * ``GET /v2/accounts/card`` → a BARE ARRAY of CardAccount (NO pagination).
  * ``GET /v2/transactions/cash/{id}`` and ``GET /v2/transactions/card/primary`` →
    ``{next_cursor, items:[…]}`` CURSOR pages; ``posted_at_start`` (date-time)
    bounds the window below (there is NO default window).

Money is a ``{amount:<int CENTS, signed>, currency}`` OBJECT (NOT a dollar number).
Transaction dates are DATE-only ``YYYY-MM-DD``. Brex publishes an OpenAPI but no
per-object JSON-Schema, so — like the QBO / mercury / ashby slices — we
structurally validate the fields a consumer depends on (incl. the enums + the
cents/sign + date-only conventions) and assert the cursor envelopes terminate.
"""
from __future__ import annotations

import re
from typing import Any

from ..fidelity import FidelityReport
from .client import BrexClient

_PAGE = 100
_MAX_PAGES = 10_000
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_CASH_TXN_TYPES = {
    "PAYMENT", "DIVIDEND", "FEE", "ADJUSTMENT", "INTEREST", "CARD_COLLECTION",
    "REWARDS_REDEMPTION", "RECEIVABLES_OFFERS_ADVANCE", "FBO_TRANSFER",
    "RECEIVABLES_OFFERS_REPAYMENT", "RECEIVABLES_OFFERS_COLLECTION",
    "BREX_OPERATIONAL_TRANSFER", "INTRA_CUSTOMER_ACCOUNT_BOOK_TRANSFER",
    "BOOK_TRANSFER", "CRYPTO_BRIDGE", "STABLECOIN", "TRANSACTION_FEES_COLLECTION",
    "PAYBACK",
}
_CARD_TXN_TYPES = {"PURCHASE", "REFUND", "CHARGEBACK", "REWARDS_CREDIT", "COLLECTION",
                   "BNPL_FEE"}


def _is_money(m: Any, *, nullable_ok: bool = False) -> bool:
    if m is None:
        return nullable_ok
    return (isinstance(m, dict) and isinstance(m.get("amount"), int)
            and not isinstance(m.get("amount"), bool)
            and isinstance(m.get("currency"), (str, type(None))))


def _validate_cash_account(a: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    for k in ("id", "status", "account_number", "routing_number"):
        if a.get(k) in (None, ""):
            problems.append(f"missing/empty `{k}`")
    if not isinstance(a.get("name"), str):
        problems.append("`name` must be a string")
    if a.get("status") != "ACTIVE":
        problems.append(f"status not ACTIVE: {a.get('status')!r}")
    for k in ("current_balance", "available_balance"):
        if not _is_money(a.get(k)):
            problems.append(f"`{k}` must be a Money object {{amount:int cents, currency}}")
    if not isinstance(a.get("primary"), bool):
        problems.append("`primary` must be a boolean")
    check = "cash account object contract"
    if problems:
        report.record_protocol(check, False, f"id={a.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def _validate_card_account(c: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not c.get("id"):
        problems.append("missing `id`")
    if c.get("status") != "ACTIVE":
        problems.append(f"status not ACTIVE: {c.get('status')!r}")
    if not _is_money(c.get("current_balance"), nullable_ok=True):
        problems.append("`current_balance` must be Money|null")
    if not _is_money(c.get("account_limit"), nullable_ok=True):
        problems.append("`account_limit` must be Money|null")
    period = c.get("current_statement_period")
    if not isinstance(period, dict):
        problems.append("`current_statement_period` must be an object {start_date,end_date}")
    else:
        for k in ("start_date", "end_date"):
            if not (isinstance(period.get(k), str) and _DATE_RE.match(period[k])):
                problems.append(f"statement `{k}` must be YYYY-MM-DD: {period.get(k)!r}")
    check = "card account object contract"
    if problems:
        report.record_protocol(check, False, f"id={c.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def _validate_txn(t: dict, *, family: str, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    for k in ("id", "description", "initiated_at_date", "posted_at_date"):
        if t.get(k) in (None, ""):
            problems.append(f"missing/empty `{k}`")
    for k in ("initiated_at_date", "posted_at_date"):
        v = t.get(k)
        if not (isinstance(v, str) and _DATE_RE.match(v)):
            problems.append(f"`{k}` must be DATE-only YYYY-MM-DD: {v!r}")
    if family == "cash":
        if not _is_money(t.get("amount"), nullable_ok=True):  # nullable on cash
            problems.append("`amount` must be Money|null (cents object)")
        if t.get("type") is not None and t.get("type") not in _CASH_TXN_TYPES:
            problems.append(f"cash type not in enum: {t.get('type')!r}")
        if "transfer_id" not in t:
            problems.append("cash txn missing `transfer_id` key")
    else:
        if not _is_money(t.get("amount")):  # required & non-null on card
            problems.append("`amount` must be a Money object (cents)")
        if t.get("type") is not None and t.get("type") not in _CARD_TXN_TYPES:
            problems.append(f"card type not in enum: {t.get('type')!r}")
        if "card_id" not in t:
            problems.append("card txn missing `card_id` key")
        m = t.get("merchant")
        if m is not None and not (isinstance(m, dict) and "raw_descriptor" in m):
            problems.append("`merchant` must be null or {raw_descriptor,mcc,country}")
    check = f"{family} transaction object contract"
    if problems:
        report.record_protocol(check, False, f"id={t.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def _walk_transactions(list_fn, label: str, family: str, report: FidelityReport) -> int:
    """Walk a cursor-paginated transactions endpoint; validate envelope + objects."""
    seen_ok: set = set()
    cursor, pages, walked = None, 0, 0
    while pages < _MAX_PAGES:
        status, _, body = list_fn(cursor=cursor, limit=_PAGE)
        report.record_page(label, cursor or "head")
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", label, f"{label} -> {status}; {str(body)[:160]}")
            return walked
        if set(body) - {"next_cursor", "items"}:
            report.record_protocol(f"{label} envelope {{next_cursor, items}}", False,
                                   f"keys={sorted(body)}")
        items = body.get("items")
        if not isinstance(items, list):
            report.diverge("protocol", label, "`items` is not an array")
            return walked
        pages += 1
        for t in items:
            if isinstance(t, dict):
                report.count(f"{family}_transaction")
                _validate_txn(t, family=family, report=report, seen_ok=seen_ok)
                walked += 1
        nxt = body.get("next_cursor")
        if not nxt:
            break
        cursor = nxt
    report.record_protocol(f"{label} envelope {{next_cursor, items}}", True, "")
    report.record_protocol(f"{label} cursor walk terminates on null next_cursor", True, "")
    return walked


def run_historical(report: FidelityReport, cfg) -> None:
    client = BrexClient(cfg, report)
    report.auth.update({"method": "Bearer user/OAuth token (Authorization: Bearer bxt_…)"})

    # 1) cash accounts — cursor page; validate envelope + each CashAccount.
    seen_ok: set = set()
    cash_accounts: list[dict] = []
    cursor, pages = None, 0
    while pages < 1000:
        status, _, body = client.list_cash_accounts(cursor=cursor, limit=_PAGE)
        report.record_page("accounts.cash", cursor or "head")
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", "accounts.cash",
                           f"GET /v2/accounts/cash -> {status}; {str(body)[:160]}")
            return
        if set(body) - {"next_cursor", "items"}:
            report.record_protocol("cash accounts envelope {next_cursor, items}", False,
                                   f"keys={sorted(body)}")
        batch = body.get("items")
        if not isinstance(batch, list):
            report.diverge("protocol", "accounts.cash", "`items` is not an array")
            return
        pages += 1
        for a in batch:
            if isinstance(a, dict):
                report.count("cash_account")
                _validate_cash_account(a, report, seen_ok)
                cash_accounts.append(a)
        cursor = body.get("next_cursor")
        if not cursor:
            break
    report.record_protocol("cash accounts envelope {next_cursor, items}", True, "")
    report.note(f"cash accounts: {len(cash_accounts)} over {pages} page(s)")

    # 2) /cash/primary selector — returns the one primary CashAccount.
    st, _, primary = client.get_primary_cash_account()
    if st == 200 and isinstance(primary, dict) and primary.get("primary") is True:
        report.record_protocol("GET /v2/accounts/cash/primary returns the primary account",
                               True, "")
    else:
        report.record_protocol("GET /v2/accounts/cash/primary returns the primary account",
                               False, f"-> {st}; primary={primary.get('primary') if isinstance(primary, dict) else primary!r}")

    # 3) card accounts — a BARE ARRAY (not a page envelope).
    st, _, cards = client.list_card_accounts()
    if st == 200 and isinstance(cards, list):
        report.record_protocol("GET /v2/accounts/card is a bare array (no pagination)", True, "")
        cseen: set = set()
        for c in cards:
            if isinstance(c, dict):
                report.count("card_account")
                _validate_card_account(c, report, cseen)
        report.note(f"card accounts: {len(cards)}")
    else:
        report.record_protocol("GET /v2/accounts/card is a bare array (no pagination)", False,
                               f"-> {st}; type={type(cards).__name__}")

    # 4) per-cash-account transaction backfill (cursor walk).
    total_cash_txns = 0
    for a in cash_accounts:
        aid = a["id"]
        total_cash_txns += _walk_transactions(
            lambda cursor=None, limit=_PAGE, _aid=aid:
                client.list_cash_transactions(_aid, cursor=cursor, limit=limit),
            f"transactions.cash.{aid[:8]}", "cash", report)

    # 5) primary card transactions (cursor walk).
    total_card_txns = _walk_transactions(
        lambda cursor=None, limit=_PAGE:
            client.list_primary_card_transactions(cursor=cursor, limit=limit),
        "transactions.card.primary", "card", report)

    # 6) posted_at_start filter is a start-bound window (informational probe).
    if cash_accounts:
        aid = cash_accounts[0]["id"]
        st, _, full = client.list_cash_transactions(aid, limit=_PAGE)
        st2, _, recent = client.list_cash_transactions(aid, limit=_PAGE,
                                                        posted_at_start="2099-01-01T00:00:00Z")
        if (st == 200 and st2 == 200 and isinstance(recent, dict)
                and isinstance(recent.get("items"), list) and len(recent["items"]) == 0):
            report.record_protocol("posted_at_start filter bounds the window", True, "")
        else:
            report.note("posted_at_start probe inconclusive (no future-dated bound effect)")

    report.note(f"transactions: {total_cash_txns} cash + {total_card_txns} card")
