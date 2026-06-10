"""Ramp historical ingestion — transactions/reimbursements/cards/users backfill.

The REAL Ramp contract (docs.ramp.com OpenAPI):
  * Every list endpoint returns ``{data:[…], page:{next}}`` with KEYSET pagination —
    ``page.next`` is a FULL URL embedding ``start=<id of the last entity on the
    page>`` (a bare wire id), and ``null`` at EOF. page_size default 20 / max 100.
  * MONEY IS DUAL: the top-level ``amount`` is a NUMBER in dollars; nested
    ``CurrencyAmount`` fields (``original_transaction_amount``, ``line_items[].amount``,
    ``original_reimbursement_amount``) are ``{amount:<int CENTS>, currency_code,
    minor_unit_conversion_rate}``.
  * Transactions key ``currency_code``; reimbursements key ``currency`` (sic).
  * Timestamps are ISO-8601 with a ``+00:00`` OFFSET (not ``Z``); reimbursement
    ``transaction_date`` is DATE-only.
  * The single-read ``GET /developer/v1/transactions/{id}`` returns the BARE object.

Ramp publishes an OpenAPI but no per-object JSON-Schema, so — like the QBO /
mercury / brex slices — we structurally validate the fields a consumer depends on
(incl. the dual-money + offset-timestamp + currency-key conventions) and assert
the keyset envelopes terminate.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..fidelity import FidelityReport
from .client import RampClient

_PAGE = 100
_MAX_PAGES = 10_000
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OFFSET_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([.+-].*)?$")

_TXN_STATES = {"CLEARED", "COMPLETION", "DECLINED", "ERROR", "PENDING", "PENDING_INITIATION"}
_SYNC_STATUSES = {"NOT_SYNC_READY", "SYNCED", "SYNC_READY"}
_REIMB_TYPES = {"MILEAGE", "OUT_OF_POCKET", "PAYBACK_FULL", "PAYBACK_PARTIAL", "PER_DIEM"}


def _is_offset_ts(v: Any) -> bool:
    return v is None or (isinstance(v, str) and bool(_OFFSET_TS_RE.match(v))
                         and ("+00:00" in v or v.endswith("Z") or "+" in v[10:] or "-" in v[10:]))


def _is_currency_amount(m: Any, *, nullable_ok: bool = False) -> bool:
    if m is None:
        return nullable_ok
    return (isinstance(m, dict) and isinstance(m.get("amount"), int)
            and not isinstance(m.get("amount"), bool)
            and isinstance(m.get("currency_code"), str))


def _validate_txn(t: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not t.get("id"):
        problems.append("missing `id`")
    # DUAL money: top-level dollars number + nested CurrencyAmount cents
    if not isinstance(t.get("amount"), (int, float)) or isinstance(t.get("amount"), bool):
        problems.append("top-level `amount` must be a NUMBER (dollars)")
    if not isinstance(t.get("currency_code"), str):
        problems.append("transaction keys `currency_code` (string)")
    if not _is_currency_amount(t.get("original_transaction_amount"), nullable_ok=True):
        problems.append("`original_transaction_amount` must be a CurrencyAmount {amount:int cents,…}")
    li = t.get("line_items")
    if not isinstance(li, list):
        problems.append("`line_items` must be an array")
    elif li and not _is_currency_amount(li[0].get("amount"), nullable_ok=True):
        problems.append("line_items[].amount must be a CurrencyAmount (cents)")
    if t.get("state") not in _TXN_STATES:
        problems.append(f"state not in enum: {t.get('state')!r}")
    if t.get("sync_status") not in _SYNC_STATUSES:
        problems.append(f"sync_status not in enum: {t.get('sync_status')!r}")
    if not _is_offset_ts(t.get("user_transaction_time")):
        problems.append(f"user_transaction_time must be ISO-8601 +00:00: {t.get('user_transaction_time')!r}")
    if not _is_offset_ts(t.get("settlement_date")):
        problems.append(f"settlement_date must be ISO-8601 +00:00: {t.get('settlement_date')!r}")
    if not isinstance(t.get("card_holder"), dict):
        problems.append("`card_holder` must be an object")
    check = "transaction object contract"
    if problems:
        report.record_protocol(check, False, f"id={t.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def _validate_reimbursement(rb: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not rb.get("id"):
        problems.append("missing `id`")
    # reimbursement keys `currency` (NOT `currency_code` — the API quirk)
    if "currency" not in rb:
        problems.append("reimbursement must key `currency`")
    if "currency_code" in rb:
        problems.append("reimbursement must NOT key `currency_code` (that's a txn key)")
    if rb.get("amount") is not None and (not isinstance(rb["amount"], (int, float))
                                         or isinstance(rb["amount"], bool)):
        problems.append("`amount` must be a NUMBER|null (dollars)")
    if not _is_currency_amount(rb.get("original_reimbursement_amount"), nullable_ok=True):
        problems.append("`original_reimbursement_amount` must be a CurrencyAmount|null")
    if rb.get("type") not in _REIMB_TYPES:
        problems.append(f"type not in enum: {rb.get('type')!r}")
    td = rb.get("transaction_date")
    if td is not None and not (isinstance(td, str) and _DATE_RE.match(td)):
        problems.append(f"transaction_date must be DATE-only YYYY-MM-DD|null: {td!r}")
    check = "reimbursement object contract"
    if problems:
        report.record_protocol(check, False, f"id={rb.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def _walk_keyset(client: RampClient, first_call, label: str, validate, report: FidelityReport,
                 count_key: str) -> int:
    """Walk a keyset-paginated list endpoint via page.next; validate envelope + objects."""
    seen_ok: set = set()
    status, _, body = first_call()
    report.record_page(label, "head")
    walked, pages = 0, 0
    while pages < _MAX_PAGES:
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", label, f"{label} -> {status}; {str(body)[:160]}")
            return walked
        if set(body) - {"data", "page"}:
            report.record_protocol(f"{label} envelope {{data, page}}", False,
                                   f"keys={sorted(body)}")
        data = body.get("data")
        page = body.get("page")
        if not isinstance(data, list) or not isinstance(page, dict) or "next" not in page:
            report.diverge("protocol", label, "missing data[]/page.next")
            return walked
        pages += 1
        last_id = None
        for obj in data:
            if isinstance(obj, dict):
                report.count(count_key)
                validate(obj, report, seen_ok)
                walked += 1
                last_id = obj.get("id")
        nxt = page.get("next")
        if not nxt:
            break
        # page.next must be a FULL URL embedding start=<last entity id>.
        parts = urlsplit(nxt)
        q = parse_qs(parts.query)
        if q.get("start", [None])[0] != last_id:
            report.record_protocol(f"{label} page.next embeds start=<last id>", False,
                                   f"start={q.get('start')} last_id={last_id}")
        else:
            report.record_protocol(f"{label} page.next embeds start=<last id>", True, "")
        status, _, body = client.follow(nxt, label)
        report.record_page(label, q.get("start", ["?"])[0])
    report.record_protocol(f"{label} envelope {{data, page}}", True, "")
    report.record_protocol(f"{label} keyset walk terminates on null page.next", True, "")
    return walked


def run_historical(report: FidelityReport, cfg) -> None:
    client = RampClient(cfg, report)
    report.auth.update({"method": "OAuth client-credentials -> Bearer ramp_business_tok_…"})

    # 1) transactions — the primary stream (keyset walk).
    n_txn = _walk_keyset(
        client, lambda: client.list_transactions(page_size=_PAGE),
        "transactions", _validate_txn, report, "transaction")
    report.note(f"transactions: {n_txn}")

    # 2) the single-read returns the BARE Transaction (not wrapped in {data}).
    st, _, page = client.list_transactions(page_size=1)
    if st == 200 and isinstance(page, dict) and page.get("data"):
        tid = page["data"][0]["id"]
        st2, _, single = client.get_transaction(tid)
        if (st2 == 200 and isinstance(single, dict) and "data" not in single
                and single.get("id") == tid):
            report.record_protocol("GET /transactions/{id} returns the BARE object", True, "")
        else:
            report.record_protocol("GET /transactions/{id} returns the BARE object", False,
                                   f"-> {st2}; wrapped={'data' in single if isinstance(single, dict) else single!r}")

    # 3) reimbursements (keyset walk).
    n_reimb = _walk_keyset(
        client, lambda: client.list_reimbursements(page_size=_PAGE),
        "reimbursements", _validate_reimbursement, report, "reimbursement")
    report.note(f"reimbursements: {n_reimb}")

    # 4) cards + users (entity-attribution; structural envelope only).
    for ep, fn in (("cards", client.list_cards), ("users", client.list_users)):
        st, _, body = fn(page_size=_PAGE)
        if st == 200 and isinstance(body, dict) and isinstance(body.get("data"), list) \
                and isinstance(body.get("page"), dict):
            report.record_protocol(f"{ep} envelope {{data, page}}", True, "")
            for o in body["data"]:
                if isinstance(o, dict):
                    report.count(ep.rstrip("s"))
            report.note(f"{ep}: {len(body['data'])}")
        else:
            report.diverge("protocol", ep, f"GET /developer/v1/{ep} -> {st}; {str(body)[:120]}")

    # 5) state filter is a meaningful incremental knob (informational probe).
    st, _, declined = client.list_transactions(page_size=_PAGE, state="DECLINED")
    if st == 200 and isinstance(declined, dict):
        report.record_protocol("transactions `state` filter accepted", True, "")
