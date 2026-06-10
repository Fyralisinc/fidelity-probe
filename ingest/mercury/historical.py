"""Mercury historical ingestion — accounts + per-account transactions backfill.

``GET /accounts`` returns ``{accounts:[…], page:{nextPage,previousPage}}`` (a UUID
cursor, not offset). For each account, ``GET /account/{id}/transactions`` returns
``{total:N, transactions:[…]}`` paged by **offset** — and **defaults to the last
30 days** unless ``start`` is given, so a full backfill passes an explicit wide
``start``. Mercury publishes no machine-readable schema, so — like the QBO/Grafana
slices — we structurally validate the fields a consumer depends on (incl. the
enums and the dollar/sign conventions) and assert the envelopes + offset
pagination terminate against ``total``.
"""
from __future__ import annotations

from typing import Any

from ..fidelity import FidelityReport
from .client import MercuryClient

_PAGE = 500
_MAX_PAGES = 10_000  # safety bound
_WIDE_START = "2000-01-01"  # bypass the 30-day default to backfill all history

_ACCOUNT_STATUSES = {"active", "deleted", "pending", "archived"}
_ACCOUNT_TYPES = {"mercury", "external", "recipient"}
_TXN_STATUSES = {"pending", "sent", "cancelled", "failed", "reversed", "blocked"}
_TXN_KINDS = {
    "externalTransfer", "internalTransfer", "outgoingPayment", "creditCardCredit",
    "creditCardTransaction", "debitCardCredit", "debitCardTransaction",
    "cardInternationalTransactionFee", "cardInternationalTransactionFeeRebate",
    "cardInternationalTransactionFeeReversal",
    "cardInternationalTransactionFeeRebateReversal", "incomingDomesticWire",
    "checkDeposit", "incomingInternationalWire", "treasuryTransfer",
    "currencyCloudReturn", "wireFee", "personalBankingSubscriptionFee",
    "billingEngineSubscriptionFee", "expenseReimbursement",
    "exogenousWireDrawdown", "other",
}


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _rfc3339_z(x: Any) -> bool:
    return isinstance(x, str) and x.endswith("Z") and "T" in x


def _validate_account(a: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    for k in ("id", "accountNumber", "routingNumber", "name", "status", "type",
              "createdAt", "kind", "legalBusinessName"):
        if not a.get(k):
            problems.append(f"missing/empty `{k}`")
    if a.get("status") not in _ACCOUNT_STATUSES:
        problems.append(f"status not in enum: {a.get('status')!r}")
    if a.get("type") not in _ACCOUNT_TYPES:
        problems.append(f"type not in enum: {a.get('type')!r}")
    for k in ("availableBalance", "currentBalance"):
        if not _is_num(a.get(k)):
            problems.append(f"`{k}` must be a number (dollars)")
    if "dashboardLink" not in a:
        problems.append("missing `dashboardLink`")
    if not _rfc3339_z(a.get("createdAt")):
        problems.append(f"`createdAt` must be RFC3339 UTC Z: {a.get('createdAt')!r}")
    check = "account object contract"
    if problems:
        report.record_protocol(check, False, f"id={a.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check)
        report.record_protocol(check, True, "")


def _validate_txn(t: dict, account_id: str, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    for k in ("id", "createdAt", "estimatedDeliveryDate", "status", "counterpartyId",
              "counterpartyName", "kind", "dashboardLink", "accountId"):
        if t.get(k) in (None, ""):
            problems.append(f"missing/empty `{k}`")
    if not _is_num(t.get("amount")):
        problems.append("`amount` must be a number (signed dollars)")
    if t.get("status") not in _TXN_STATUSES:
        problems.append(f"status not in enum: {t.get('status')!r}")
    if t.get("kind") not in _TXN_KINDS:
        problems.append(f"kind not in enum: {t.get('kind')!r}")
    if t.get("accountId") != account_id:
        problems.append(f"accountId {t.get('accountId')!r} != owning account {account_id!r}")
    # required arrays + required bools
    for arr in ("glAllocations", "attachments", "relatedTransactions"):
        if not isinstance(t.get(arr), list):
            problems.append(f"`{arr}` must be an array")
    for b in ("compliantWithReceiptPolicy", "hasGeneratedReceipt"):
        if not isinstance(t.get(b), bool):
            problems.append(f"`{b}` must be a boolean")
    # a pending transaction has not posted; postedAt is null while pending
    if t.get("status") == "pending" and t.get("postedAt") not in (None,):
        problems.append("pending transaction should have postedAt=null")
    check = "transaction object contract"
    if problems:
        report.record_protocol(check, False, f"id={t.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check)
        report.record_protocol(check, True, "")


def _page_transactions(client: MercuryClient, account_id: str,
                       report: FidelityReport) -> int:
    seen_ok: set = set()
    offset = 0
    pages = 0
    walked = 0
    reported_total: int | None = None
    order_ok = True
    prev_created: str | None = None
    while pages < _MAX_PAGES:
        status, _, body = client.list_transactions(
            account_id, limit=_PAGE, offset=offset, start=_WIDE_START)
        report.record_page(f"transactions.{account_id[:8]}", str(offset))
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", "transactions",
                           f"list transactions -> {status}; {str(body)[:160]}")
            return walked
        if "total" not in body or not isinstance(body.get("total"), int):
            report.record_protocol("transactions envelope carries int `total`", False,
                                   f"total={body.get('total')!r}")
        else:
            reported_total = body["total"]
        items = body.get("transactions")
        if not isinstance(items, list):
            report.diverge("protocol", "transactions",
                           "`transactions` is not an array")
            return walked
        pages += 1
        for t in items:
            if not isinstance(t, dict):
                report.diverge("protocol", "transactions", "transaction element is not an object")
                continue
            report.count("transaction")
            _validate_txn(t, account_id, report, seen_ok)
            c = t.get("createdAt")
            if isinstance(c, str) and prev_created is not None and c > prev_created and order_ok:
                order_ok = False
                report.record_protocol("transactions newest-first ordering", False,
                                       f"createdAt {c} > previous {prev_created}")
            if isinstance(c, str):
                prev_created = c
            walked += 1
        if len(items) < _PAGE:
            break  # short page = EOF
        offset += len(items)
    if order_ok:
        report.record_protocol("transactions newest-first ordering", True, "")
    if reported_total is not None and reported_total != walked:
        report.record_protocol("transactions offset pagination matches `total`", False,
                               f"total={reported_total} but walked {walked}")
    else:
        report.record_protocol("transactions offset pagination matches `total`", True, "")
    return walked


def run_historical(report: FidelityReport, cfg) -> None:
    client = MercuryClient(cfg, report)
    report.auth.update({"method": "API-token Bearer (Authorization: Bearer secret-token:…)"})

    # 1) accounts — cursor-paginated; validate the envelope + each account object.
    seen_ok: set = set()
    accounts: list[dict] = []
    cursor: str | None = None
    pages = 0
    while pages < 1000:
        status, _, body = client.list_accounts(limit=_PAGE, start_after=cursor)
        report.record_page("accounts", cursor or "head")
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", "accounts",
                           f"GET /accounts -> {status}; {str(body)[:160]}")
            return
        if "accounts" not in body or "page" not in body:
            report.record_protocol("accounts envelope {accounts, page}", False,
                                   f"keys={sorted(body)}")
        batch = body.get("accounts") or []
        if not isinstance(batch, list):
            report.diverge("protocol", "accounts", "`accounts` is not an array")
            return
        pages += 1
        for a in batch:
            if isinstance(a, dict):
                report.count("account")
                _validate_account(a, report, seen_ok)
                accounts.append(a)
        page = body.get("page") or {}
        cursor = page.get("nextPage")
        if not cursor or not batch:
            break
    report.record_protocol("accounts envelope {accounts, page}", True, "")
    report.note(f"accounts: {len(accounts)} over {pages} page(s)")

    # 2) a no-start probe documents the 30-day default window (informational).
    if accounts:
        aid = accounts[0]["id"]
        st, _, probe = client.list_transactions(aid, limit=_PAGE)  # no start
        if st == 200 and isinstance(probe, dict):
            report.note(f"default (no-`start`) window on {aid[:8]} returned "
                        f"{probe.get('total')} txn(s) — Mercury defaults to the last 30 days; "
                        f"a full backfill must pass an explicit wide `start`.")

    # 3) per-account transaction backfill (explicit wide start).
    total_txns = 0
    for a in accounts:
        n = _page_transactions(client, a["id"], report)
        total_txns += n
    report.note(f"transactions: {total_txns} across {len(accounts)} account(s)")
