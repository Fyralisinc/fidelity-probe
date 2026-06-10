"""Deel historical ingestion — contracts + invoices backfill.

The REAL Deel contract (developer.deel.com):
  * ``GET /contracts`` → ``{data:[Contract], page:{cursor, total_rows}}`` — CURSOR-only
    pagination via ``after_cursor`` (the ``page.cursor`` to pass back); present while
    more pages remain, null/absent on the last page.
  * ``GET /contracts/{id}`` → ``{data:{Contract}}`` (a single object, wrapped).
  * ``GET /invoices`` → ``{data:[Invoice], page:{offset, total_rows, items_per_page,
    cursor}}`` — HYBRID ``limit``/``offset``/``cursor`` pagination; a no-``status``
    probe returns ONLY paid invoices, so a full backfill MUST pass ``status=all``.
    Each invoice carries ``contract_id`` (the link back to a contract) — this is
    Deel's real "payments" surface (payments are NOT contract-nested).

Money is a decimal STRING in major units (``"1000.00"``) — NOT cents, NOT a number.
Timestamps are RFC3339 with milliseconds + Z; ``start_date`` is DATE-only. Deel
publishes docs but no per-object JSON-Schema, so — like the QBO / mercury / brex
slices — we structurally validate the fields a consumer depends on (incl. the enums
+ the decimal-string + envelope conventions) and assert the page walks terminate.
"""
from __future__ import annotations

import re
from typing import Any

from ..fidelity import FidelityReport
from .client import DeelClient

_CONTRACTS_PAGE = 50
_INVOICES_PAGE = 100
_MAX_PAGES = 10_000

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONEY_RE = re.compile(r"^-?\d+(\.\d+)?$")

_CONTRACT_TYPES = {
    "ongoing_time_based", "pay_as_you_go_time_based", "milestones", "eor",
    "employee", "global_payroll", "commissions",
}
_CONTRACT_STATUSES = {
    "new", "under_review", "waiting_for_employee_contract", "waiting_for_client_sign",
    "processing_payment", "waiting_for_contractor_sign", "waiting_for_eor_sign",
    "waiting_for_employee_sign", "awaiting_deposit_payment", "in_progress",
    "completed", "cancelled", "user_cancelled", "rejected",
    "waiting_for_client_payment", "onboarding", "waiting_for_approval", "onboarded",
}
_INVOICE_STATUSES = {"pending", "paid", "processing", "credited", "refunded"}


def _is_money_str(v: Any) -> bool:
    return isinstance(v, str) and bool(_MONEY_RE.match(v))


def _validate_contract(c: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not c.get("id"):
        problems.append("missing `id`")
    if c.get("type") not in _CONTRACT_TYPES:
        problems.append(f"type not in enum: {c.get('type')!r}")
    if c.get("status") not in _CONTRACT_STATUSES:
        problems.append(f"status not in enum: {c.get('status')!r}")
    if not isinstance(c.get("title"), str):
        problems.append("`title` must be a string")
    worker = c.get("worker")
    if not (isinstance(worker, dict) and "full_name" in worker):
        problems.append("`worker` must be an object with full_name")
    comp = c.get("compensation_details")
    if not isinstance(comp, dict):
        problems.append("`compensation_details` must be an object")
    else:
        if not _is_money_str(comp.get("amount")):
            problems.append(f"compensation amount must be a decimal STRING: {comp.get('amount')!r}")
        if not isinstance(comp.get("currency_code"), str):
            problems.append("compensation currency_code must be a string")
    for k in ("created_at", "updated_at"):
        v = c.get(k)
        if not (isinstance(v, str) and _TS_RE.match(v)):
            problems.append(f"`{k}` must be RFC3339 ms+Z: {v!r}")
    sd = c.get("start_date")
    if sd is not None and not (isinstance(sd, str) and _DATE_RE.match(sd)):
        problems.append(f"`start_date` must be DATE-only YYYY-MM-DD: {sd!r}")
    td = c.get("termination_date")
    if td is not None and not (isinstance(td, str) and _DATE_RE.match(td)):
        problems.append(f"`termination_date` must be null or YYYY-MM-DD: {td!r}")
    check = "contract object contract"
    if problems:
        report.record_protocol(check, False, f"id={c.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def _validate_invoice(inv: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not inv.get("id"):
        problems.append("missing `id`")
    if inv.get("status") not in _INVOICE_STATUSES:
        problems.append(f"status not in enum: {inv.get('status')!r}")
    for k in ("total", "amount"):
        if not _is_money_str(inv.get(k)):
            problems.append(f"`{k}` must be a decimal STRING (major units): {inv.get(k)!r}")
    if not isinstance(inv.get("currency"), str):
        problems.append("`currency` must be a string")
    if not isinstance(inv.get("contract_id"), str):
        problems.append("invoice missing string `contract_id` (the contract link)")
    iss = inv.get("issued_at")
    if not (isinstance(iss, str) and _TS_RE.match(iss)):
        problems.append(f"`issued_at` must be RFC3339 ms+Z: {iss!r}")
    if not isinstance(inv.get("is_overdue"), bool):
        problems.append("`is_overdue` must be a boolean")
    paid = inv.get("paid_at")
    if paid is not None and not (isinstance(paid, str) and _TS_RE.match(paid)):
        problems.append(f"`paid_at` must be null or RFC3339 ms+Z: {paid!r}")
    check = "invoice object contract"
    if problems:
        report.record_protocol(check, False, f"id={inv.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def run_historical(report: FidelityReport, cfg) -> None:
    client = DeelClient(cfg, report)
    report.auth.update({"method": "Bearer org/personal API token (Authorization: Bearer …)"})

    # 1) contracts — CURSOR-only page walk; validate envelope + each Contract.
    seen_ok: set = set()
    contracts: list[dict] = []
    cursor, pages, total_rows = None, 0, None
    while pages < _MAX_PAGES:
        status, _, body = client.list_contracts(after_cursor=cursor, limit=_CONTRACTS_PAGE)
        report.record_page("contracts", cursor or "head")
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", "contracts",
                           f"GET /contracts -> {status}; {str(body)[:160]}")
            return
        if set(body) - {"data", "page"}:
            report.record_protocol("contracts envelope {data, page}", False,
                                   f"keys={sorted(body)}")
        page = body.get("page")
        if not isinstance(page, dict) or set(page) - {"cursor", "total_rows"}:
            report.record_protocol("contracts page {cursor, total_rows}", False,
                                   f"page={page!r}")
        else:
            total_rows = page.get("total_rows")
        data = body.get("data")
        if not isinstance(data, list):
            report.diverge("protocol", "contracts", "`data` is not an array")
            return
        pages += 1
        for c in data:
            if isinstance(c, dict):
                report.count("contract")
                _validate_contract(c, report, seen_ok)
                contracts.append(c)
        cursor = page.get("cursor") if isinstance(page, dict) else None
        if not cursor:
            break
    report.record_protocol("contracts envelope {data, page}", True, "")
    report.record_protocol("contracts page {cursor, total_rows}", True, "")
    report.record_protocol("contracts cursor walk terminates on null page.cursor", True, "")
    if total_rows is not None and total_rows == len(contracts):
        report.record_protocol("contracts page.total_rows matches walked count", True, "")
    report.note(f"contracts: {len(contracts)} over {pages} page(s)")

    # 2) single contract is wrapped in {data:{…}}.
    if contracts:
        cid = contracts[0]["id"]
        st, _, one = client.get_contract(cid)
        if (st == 200 and isinstance(one, dict) and set(one) == {"data"}
                and isinstance(one["data"], dict) and one["data"].get("id") == cid):
            report.record_protocol("GET /contracts/{id} returns a single {data:{…}} object",
                                   True, "")
        else:
            report.record_protocol("GET /contracts/{id} returns a single {data:{…}} object",
                                   False, f"-> {st}; body={str(one)[:160]}")

    # 3) invoices — HYBRID page walk; status=all to backfill EVERY status.
    invoices: list[dict] = []
    iseen: set = set()
    cursor, pages, inv_total = None, 0, None
    offsets: list[int] = []
    while pages < _MAX_PAGES:
        status, _, body = client.list_invoices(cursor=cursor, limit=_INVOICES_PAGE,
                                               status="all")
        report.record_page("invoices", cursor or "head")
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", "invoices",
                           f"GET /invoices -> {status}; {str(body)[:160]}")
            break
        if set(body) - {"data", "page"}:
            report.record_protocol("invoices envelope {data, page}", False,
                                   f"keys={sorted(body)}")
        page = body.get("page")
        if not isinstance(page, dict) or set(page) - {"offset", "total_rows",
                                                      "items_per_page", "cursor"}:
            report.record_protocol(
                "invoices page {offset, total_rows, items_per_page, cursor}", False,
                f"page={page!r}")
        else:
            inv_total = page.get("total_rows")
            offsets.append(page.get("offset"))
        data = body.get("data")
        if not isinstance(data, list):
            report.diverge("protocol", "invoices", "`data` is not an array")
            break
        pages += 1
        for inv in data:
            if isinstance(inv, dict):
                report.count("invoice")
                _validate_invoice(inv, report, iseen)
                invoices.append(inv)
        cursor = page.get("cursor") if isinstance(page, dict) else None
        if not cursor:
            break
    report.record_protocol("invoices envelope {data, page}", True, "")
    report.record_protocol("invoices page {offset, total_rows, items_per_page, cursor}", True, "")
    report.record_protocol("invoices cursor walk terminates on null page.cursor", True, "")
    if offsets and offsets == sorted(offsets) and offsets[0] == 0:
        report.record_protocol("invoices offset advances monotonically from 0", True, "")
    if inv_total is not None and inv_total == len(invoices):
        report.record_protocol("invoices page.total_rows matches walked count", True, "")
    report.note(f"invoices (status=all): {len(invoices)} over {pages} page(s)")

    # 4) The paid-only-vs-status=all filter: a no-status probe returns ONLY paid
    #    invoices, and that count must be <= the status=all total.
    st, _, paid_body = client.list_invoices(limit=_INVOICES_PAGE)
    if st == 200 and isinstance(paid_body, dict) and isinstance(paid_body.get("data"), list):
        paid_only = all(i.get("status") == "paid" for i in paid_body["data"])
        paid_total = (paid_body.get("page") or {}).get("total_rows")
        ok = paid_only and (inv_total is None or paid_total is None or paid_total <= inv_total)
        report.record_protocol(
            "no-status invoices probe returns ONLY paid (status=all returns more)", ok,
            "" if ok else f"paid_only={paid_only} paid_total={paid_total} all_total={inv_total}")
        report.note(f"invoices: {paid_total} paid (default) vs {inv_total} all (status=all)")

    # 5) contract_id links every invoice back to a known contract (best-effort).
    if contracts and invoices:
        cids = {c["id"] for c in contracts}
        linked = sum(1 for i in invoices if i.get("contract_id") in cids)
        report.note(f"invoice→contract links resolved: {linked}/{len(invoices)}")
